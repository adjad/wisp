"""Intel v5 full remote mode (WISP_REMOTE_FULL=1) and the Aria demo switch.

Synthetic only: a fresh WISP_HOME, sockets forbidden, no app import that
starts anything. Founder/read-only behavior is covered by the existing
test_intel_remote_* and test_founder_demo suites, which must stay green.
"""
import ast
import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

if not os.environ.get('WISP_HOME') or Path(os.environ['WISP_HOME']).resolve() == Path.home() / '.moe':
    raise RuntimeError('Use a fresh disposable WISP_HOME for Intel synthetic tests')

import service.main  # noqa: F401,E402 — registers every built-in tool
from fastapi import HTTPException  # noqa: E402

from service.config.endpoints import EndpointConfigurationError  # noqa: E402
from service.safety import grants, policy as safety  # noqa: E402
from service.tools.registry import REGISTRY, run_tool  # noqa: E402
from tests.test_intel_remote_only_main import selected  # noqa: E402
from tests.test_intel_remote_policy import RemoteFixture, policy  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WRITES_AND_SENDS = ("send_email", "send_message", "reply_to_email", "forward_email", "schedule_send",
                    "draft_email", "draft_message", "add_reminder", "update_reminder", "complete_reminder",
                    "add_calendar_event", "update_event", "cancel_event", "create_note", "append_note",
                    "manage_contacts", "set_timer", "run_shell")
SENDS = ("send_email", "send_message", "reply_to_email", "forward_email", "schedule_send")


def _profile_dict(profile):
    raw = asdict(profile)
    raw['generation_roles'] = list(raw['generation_roles'])
    raw['tool_capabilities'] = list(raw['tool_capabilities'])
    return raw


class FullFixture(RemoteFixture):
    """RemoteFixture, but launched as the full build with the 32k contract."""

    def setUp(self):
        super().setUp()
        self.cfg = {'inference': {'remote_only': _profile_dict(policy.REMOTE_FULL_PROFILE)}}
        for p in (patch.object(policy, '_FULL_MODE', '1'), patch.object(policy, '_FOUNDER_MODE', None)):
            p.start()
            self.addCleanup(p.stop)
        policy.remote_only_profile.cache_clear()
        self.addCleanup(policy.remote_only_profile.cache_clear)
        tmp = Path(os.environ['WISP_HOME']) / f'grants-{id(self)}.json'
        for p in (patch.object(grants, 'GRANTS_PATH', tmp), patch.object(grants, '_CACHE', {}),
                  patch.object(safety, '_FULL_ACCESS', False)):
            p.start()
            self.addCleanup(p.stop)


class ContractChecks(FullFixture):
    def test_full_contract_values(self):
        p = policy.remote_only_profile()
        self.assertIs(p, policy.REMOTE_FULL_PROFILE)
        self.assertTrue(policy.remote_full_mode())
        self.assertFalse(policy.remote_read_only_mode())
        self.assertEqual((p.context_window, p.aggregate_bytes, p.request_bytes, p.messages, p.max_tokens, p.tools),
                         (32000, 131072, 524288, 200, 8192, 32))
        # Everything that pins the endpoint is identical to the read-only contract.
        base = policy.RemoteOnlyProfile()
        for key in ('base_url', 'model_id', 'credential_ref', 'endpoint_name', 'provider', 'api_prefix',
                    'readiness_timeout', 'response_bytes', 'tools', 'generation_roles', 'tool_capabilities'):
            self.assertEqual(getattr(p, key), getattr(base, key), key)
        t = policy.remote_default_target('agent')
        self.assertEqual((t.model, t.context_window, t.endpoint.credential_ref), ('qwen3.6:35b', 32000, 'keychain:gx10demo'))
        self.assertFalse(t.endpoint.managed)
        policy.validate_remote_target(t)
        self.assertEqual(p.fit_output(20000), 8192)

    def test_full_mode_refuses_the_8k_contract_and_any_drift(self):
        self.cfg = {'inference': {'remote_only': _profile_dict(policy.RemoteOnlyProfile())}}
        policy.remote_only_profile.cache_clear()
        with self.assertRaises(EndpointConfigurationError):
            policy.remote_only_profile()
        for key, val in [('context_window', 100000), ('model_id', 'other'), ('base_url', 'https://x.invalid'),
                         ('credential_ref', 'env:X'), ('tools', 64), ('aggregate_bytes', 400000)]:
            raw = _profile_dict(policy.REMOTE_FULL_PROFILE)
            raw[key] = val
            self.cfg = {'inference': {'remote_only': raw}}
            policy.remote_only_profile.cache_clear()
            with self.subTest(key=key), self.assertRaises(EndpointConfigurationError):
                policy.remote_only_profile()

    def test_launch_flag_combinations_fail_closed(self):
        for forced, full, founder in [(None, '1', None), ('0', '1', None), ('1', '1', '1'), ('1', 'yes', None), ('1', '1', '0')]:
            policy.remote_only_profile.cache_clear()
            with self.subTest(forced=forced, full=full, founder=founder), \
                    patch.object(policy, '_FORCED_MODE', forced), patch.object(policy, '_FULL_MODE', full), \
                    patch.object(policy, '_FOUNDER_MODE', founder), self.assertRaises(EndpointConfigurationError):
                policy.remote_only_profile()

    def test_full_payload_budget(self):
        p = policy.remote_only_profile()
        body = {'model': p.model_id, 'messages': [{'role': 'user', 'content': 'x' * 100_000}],
                'stream': True, 'max_tokens': 8192}
        p.validate_payload(body)  # ~100 KB fits the 131,072-byte aggregate
        for extra in [{'messages': [{'role': 'user', 'content': 'x' * 140_000}]}, {'max_tokens': 8193},
                      {'chat_template_kwargs': {}}, {'tools': [{}] * 33}]:
            with self.subTest(extra=list(extra)), self.assertRaises(EndpointConfigurationError):
                p.validate_payload({**body, **extra})


class ReadOnlyContractChecks(RemoteFixture):
    def test_read_only_accepts_either_fixed_contract(self):
        self.assertIs(policy.remote_only_profile(), policy.remote_only_profile())
        self.assertEqual(policy.remote_only_profile(), policy.RemoteOnlyProfile())
        self.assertTrue(policy.remote_read_only_mode())
        self.assertFalse(policy.remote_full_mode())
        # The v5 package ships the 32k profile for both runtime modes.
        self.cfg = {'inference': {'remote_only': _profile_dict(policy.REMOTE_FULL_PROFILE)}}
        policy.remote_only_profile.cache_clear()
        self.assertIs(policy.remote_only_profile(), policy.REMOTE_FULL_PROFILE)
        self.assertTrue(policy.remote_read_only_mode())
        # ...and the allowlist is the same seven reads either way.
        for name in WRITES_AND_SENDS + ('lookup_contact', 'read_file'):
            self.assertFalse(policy.remote_tool_allowed(name), name)
            self.assertIs(safety.decide(REGISTRY[name].category, {}, tool=name).tier, safety.Tier.DENY)
        for name in policy._READ_TOOLS:
            self.assertTrue(policy.remote_tool_allowed(name), name)


class ToolChecks(FullFixture):
    def test_full_mode_exposes_writes_and_sends(self):
        for name in WRITES_AND_SENDS:
            with self.subTest(tool=name):
                self.assertIn(name, REGISTRY)
                self.assertTrue(policy.remote_tool_allowed(name))
        # Read-only allowlist is NOT what decides full mode.
        self.assertTrue(policy.remote_tool_allowed('lookup_contact'))
        self.assertFalse(policy.remote_demo_tool_allowed('send_email'))

    def test_full_mode_excludes_only_local_runtime_tools(self):
        excluded = {n for n in REGISTRY if not policy.remote_tool_allowed(n)}
        self.assertEqual(excluded & {'create_tool', 'use_skill', 'wisp_skills', 'wisp_mcp'},
                         {'create_tool', 'use_skill', 'wisp_skills', 'wisp_mcp'} & set(REGISTRY))
        self.assertFalse(any(n in excluded for n in WRITES_AND_SENDS))
        for name in ('mcp_server_tool', 'not_a_tool', '', None, []):
            self.assertFalse(policy.remote_tool_allowed(name))
        decision = safety.decide('tool_authoring', {}, tool='create_tool')
        self.assertIs(decision.tier, safety.Tier.DENY)
        self.assertIn('on-Mac', decision.reason)
        result = asyncio.run(run_tool(REGISTRY['create_tool'], {}))
        self.assertIn('nothing was executed', result)

    def test_writes_use_the_normal_ask_and_grants(self):
        args = {'title': 'synthetic', 'when': 'tomorrow 9am'}
        d = safety.decide(REGISTRY['add_reminder'].category, args, tool='add_reminder')
        self.assertNotIn('remote', d.reason.lower())
        # A write is grantable exactly as on a Mac: once granted it no longer asks.
        if grants.is_grantable('add_reminder', args):
            self.assertTrue(grants.grant('add_reminder', args, decision='allow').get('ok'))
            self.assertIs(safety.decide(REGISTRY['add_reminder'].category, args, tool='add_reminder').tier,
                          safety.Tier.ALLOW)
        self.assertTrue(grants.grant('add_reminder', args, decision='deny', scoped=False).get('ok'))
        self.assertIs(safety.decide(REGISTRY['add_reminder'].category, args, tool='add_reminder').tier,
                      safety.Tier.DENY)

    def test_sends_confirm_every_time_and_cannot_be_standing_granted(self):
        cases = {'send_email': {'to': 'friend@example.com', 'subject': 's', 'body': 'b'},
                 'send_message': {'to': '+15555550100', 'text': 'hi'},
                 'reply_to_email': {'body': 'b'}, 'forward_email': {'to': 'friend@example.com'},
                 'schedule_send': {'channel': 'message', 'to': '+15555550100', 'body': 'hi', 'when': 'tomorrow 6pm'}}
        for name, args in cases.items():
            with self.subTest(tool=name):
                category = REGISTRY[name].category
                self.assertIn(category, safety.ALWAYS_CONFIRM_CATEGORIES)
                self.assertIs(safety.decide(category, args, tool=name).tier, safety.Tier.CONFIRM)
                self.assertFalse(grants.is_grantable(name, args))
                self.assertIs(grants.grant(name, args, decision='allow').get('ok'), False)
                # A stale allow written by hand is still ignored.
                grants._CACHE[name] = {'allow': [{'scope': ''}], 'deny': []}
                self.assertIs(safety.decide(category, args, tool=name).tier, safety.Tier.CONFIRM)
                # Full access does not lift always-confirm outbound either.
                with patch.object(safety, '_FULL_ACCESS', True):
                    self.assertIsNot(safety.decide(category, args, tool=name).tier, safety.Tier.ALLOW)


class MainChecks(FullFixture):
    def _lifespan_ns(self, fake, scheduler_run, bomb):
        return {'asyncio': asyncio, 'asynccontextmanager': asynccontextmanager,
                'remote_only_profile': policy.remote_only_profile, 'remote_full_mode': policy.remote_full_mode,
                'aria_demo_mode': policy.aria_demo_mode,
                'require_local_models_disabled': policy.require_local_models_disabled,
                'OMLXClient': lambda: fake, 'role_to_model': lambda role: 'qwen3.6:35b',
                'skills': SimpleNamespace(load=bomb), 'mcp_manager': SimpleNamespace(start=bomb, stop=bomb),
                'idle_unloader': SimpleNamespace(run=bomb), 'assistant_scheduler': SimpleNamespace(run=scheduler_run),
                'cloud_super_model_enabled': bomb, 'start_laya_warmup': bomb,
                'research_manager': SimpleNamespace(store=SimpleNamespace(purge_stale_bodies=Mock())),
                'research_cache': SimpleNamespace(purge_older_than=Mock()), 'assistant_store': object(),
                'ensure_omlx': bomb, '_omlx_cli': bomb}

    def test_full_startup_runs_scheduler_and_nothing_local(self):
        fake = SimpleNamespace(health=AsyncMock(return_value={'ok': True}), aclose=AsyncMock())
        started = asyncio.Event()

        async def scheduler_run():
            started.set()
            await asyncio.Event().wait()
        q = ModuleType('service.config.quarantine')
        q.check = Mock()
        watched = []

        async def watch(tasks, close):
            watched.extend(tasks)
            await asyncio.Event().wait()
        q.watch = watch
        identity = ModuleType('service.identity')
        identity.refuse_sandbox_on_production_port = Mock()
        host = ModuleType('service.browser.host')
        host.start_from_environment = Mock(return_value=None)
        browser = ModuleType('service.browser')
        browser.host = host
        bomb = Mock(side_effect=AssertionError('local engine/background operation forbidden'))
        ns = self._lifespan_ns(fake, scheduler_run, bomb)
        selected({'lifespan', '_sync_keep_warm', '_prepare_remote_inference'}, ns)

        async def run():
            with patch.dict('sys.modules', {'service.identity': identity, 'service.config.quarantine': q,
                                            'service.browser': browser, 'service.browser.host': host}), \
                    patch('service.config.quarantine', q):
                async with ns['lifespan'](None):
                    await asyncio.wait_for(started.wait(), 1)
                    for _ in range(10):
                        await asyncio.sleep(0)
                    self.assertEqual(len(watched), 1)  # exactly the scheduler
        asyncio.run(run())
        fake.health.assert_awaited_once()
        fake.aclose.assert_awaited_once()
        bomb.assert_not_called()

    def test_scheduler_never_starts_omlx(self):
        from service.assistant import scheduler
        bomb = AsyncMock(side_effect=AssertionError('oMLX start forbidden'))
        ensure_bomb = Mock(side_effect=AssertionError('oMLX lifecycle forbidden'))

        async def stop_after_one(_seconds):
            raise asyncio.CancelledError
        with patch('service.main.ensure_omlx', bomb), patch('service.main._omlx_cli', bomb), \
                patch('service.inference.omlx_client.OMLXClient.ensure_only', ensure_bomb), \
                patch.object(scheduler, '_maybe_daily_brief', AsyncMock()), \
                patch.object(scheduler, '_fire_scheduled_sends', AsyncMock()), \
                patch.object(scheduler, '_start_behavior_tick', Mock(return_value=None)) as tick, \
                patch.object(scheduler.asyncio, 'sleep', stop_after_one):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(scheduler.run())
        tick.assert_called_once()
        bomb.assert_not_called()
        ensure_bomb.assert_not_called()

    def test_ui_write_handlers_allowed_in_full_mode(self):
        names = {'assistant_add', 'assistant_update', 'assistant_delete', 'assistant_send_message_draft',
                 'assistant_event_claim', 'approve'}
        tree = ast.parse((ROOT / 'service/main.py').read_text())
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name in names:
                guard = node.body[1] if isinstance(node.body[0], ast.Expr) else node.body[0]
                self.assertIsInstance(guard, ast.If, node.name)
                self.assertEqual(ast.unparse(guard.test), 'remote_read_only_mode()', node.name)
        ns = {'remote_read_only_mode': policy.remote_read_only_mode, 'HTTPException': HTTPException,
              'assistant_store': SimpleNamespace(set_status=Mock(return_value=False)),
              'assistant_hub': SimpleNamespace(publish=AsyncMock())}
        selected({'assistant_update'}, ns)
        self.assertEqual(asyncio.run(ns['assistant_update']('synthetic-id', {'status': 'done'})), {'ok': False})

    def test_history_and_memory_follow_the_mac_path(self):
        source = (ROOT / 'service/main.py').read_text()
        self.assertIn('messages = ([user_msg] if super_model_cloud or remote_read_only_mode() else _tool_turn_messages(', source)
        self.assertIn('include_memory_context=not remote_read_only_mode() and _agent_memory_context_allowed(', source)
        ns = {'remote_full_mode': policy.remote_full_mode}
        selected({'_history_budget'}, ns)
        ns['_REMOTE_FULL_HISTORY_TOKENS'] = 6000
        self.assertEqual(ns['_history_budget'](policy.remote_default_target('agent')), 6000)

    def test_wisp_status_does_not_claim_local_inference(self):
        text = asyncio.run(REGISTRY['wisp_status'].func())
        self.assertIn('qwen3.6:35b', text)
        self.assertIn('GX10', text)
        self.assertNotIn('no request leaves the machine', text)


class BehaviorChecks(FullFixture):
    def test_behavior_defaults_on_real_and_kill_switch(self):
        from service.behavior import flags
        from service.assistant import behavior_runtime as br
        env = {'WISP_REMOTE_ONLY': '1', 'WISP_REMOTE_FULL': '1'}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop('WISP_BEHAVIOR', None)
            os.environ.pop('WISP_FOUNDER_DEMO', None)
            os.environ.pop('WISP_BEHAVIOR_SYNTHETIC_ONLY', None)
            self.assertTrue(flags.env_enabled())
            self.assertFalse(br.synthetic_only())
            self.assertFalse(br.founder_demo())
            self.assertTrue(policy.remote_tool_allowed('describe_me'))
            os.environ['WISP_BEHAVIOR'] = '0'
            self.assertFalse(flags.env_enabled())
            self.assertIsNone(br.get_runtime())
        with patch.dict(os.environ, {'WISP_REMOTE_FULL': '1'}, clear=False):
            os.environ.pop('WISP_REMOTE_ONLY', None)
            os.environ.pop('WISP_BEHAVIOR', None)
            os.environ.pop('WISP_FOUNDER_DEMO', None)
            self.assertFalse(flags.env_enabled())  # full flag alone is not the v5 launch

    def test_real_runtime_is_not_synthetic(self):
        from service.assistant import behavior_runtime as br
        home = Path(os.environ['WISP_HOME']) / f'behavior-{id(self)}'
        home.mkdir()
        with patch.dict(os.environ, {'WISP_REMOTE_ONLY': '1', 'WISP_REMOTE_FULL': '1'}, clear=False):
            os.environ.pop('WISP_FOUNDER_DEMO', None)
            os.environ.pop('WISP_BEHAVIOR_SYNTHETIC_ONLY', None)
            rt = br.BehaviorRuntime(home, mode='local', local_factory=lambda path: Mock())
            try:
                self.assertFalse(rt.synthetic)
                self.assertFalse(rt.founder)
            finally:
                rt.close()


def _paths_in_subprocess(extra_env: dict) -> dict:
    code = ('import importlib, json\n'
            'm = importlib.import_module\n'
            'paths, store, outbound_queue = m("service.paths"), m("service.assistant.store"), m("service.assistant.outbound_queue")\n'
            'sessions, facts, grants = m("service.memory.store"), m("service.memory.facts"), m("service.safety.grants")\n'
            'scheduler, behavior_runtime = m("service.assistant.scheduler"), m("service.assistant.behavior_runtime")\n'
            'cache_store = m("service.tools.cache_store")\n'
            'print(json.dumps({"moe": str(paths.MOE_DIR), "state": str(paths.STATE_DIR),\n'
            '  "assistant": str(store.DB_PATH), "queue": str(outbound_queue.DB_PATH),\n'
            '  "sessions": str(sessions.DB_PATH), "facts": str(facts.DB_PATH),\n'
            '  "grants": str(grants.GRANTS_PATH), "brief": str(scheduler._brief_state_path()),\n'
            '  "behavior": str(behavior_runtime._home()), "cache": str(cache_store.CACHE_DIR)}))\n')
    env = {k: v for k, v in os.environ.items()
           if k not in {'WISP_FOUNDER_DEMO', 'WISP_REMOTE_FULL', 'WISP_REMOTE_ONLY', 'WISP_ARIA_DEMO'}}
    env.update(extra_env)
    out = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


class FounderStateSeparation(RemoteFixture):
    def test_demo_and_real_state_never_share_files(self):
        real = _paths_in_subprocess({'WISP_REMOTE_ONLY': '1', 'WISP_REMOTE_FULL': '1'})
        demo = _paths_in_subprocess({'WISP_REMOTE_ONLY': '1', 'WISP_FOUNDER_DEMO': '1'})
        home = Path(os.environ['WISP_HOME']).resolve()
        self.assertEqual(Path(real['state']).resolve(), home)
        self.assertEqual(Path(demo['state']).resolve(), home / 'aria-demo')
        self.assertEqual(real['moe'], demo['moe'])  # shared home: caches and identity unchanged
        for key in ('assistant', 'queue', 'sessions', 'facts', 'grants', 'brief', 'behavior', 'cache'):
            with self.subTest(key=key):
                self.assertNotEqual(real[key], demo[key])
                self.assertTrue(Path(demo[key]).resolve().is_relative_to(home / 'aria-demo'))
                self.assertFalse(Path(real[key]).resolve().is_relative_to(home / 'aria-demo'))

    def test_demo_ignores_native_notes_reminders_contacts_pushes(self):
        tree = ast.parse((ROOT / 'service/main.py').read_text())
        notes = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'assistant_sync_notes')
        ifs = [n for n in notes.body if isinstance(n, ast.If)]
        self.assertEqual([ast.unparse(n.test) for n in ifs[:2]], ['_aria_demo()', 'enabled()'])
        self.assertIn('ignored_native_sync', ast.unparse(ifs[1]))


class SwiftModeMapping(RemoteFixture):
    def test_swift_toggle_maps_to_exactly_one_backend_mode(self):
        swiftc = '/usr/bin/swiftc'
        if not Path(swiftc).exists():
            self.skipTest('swiftc unavailable')
        out_dir = Path(os.environ['WISP_HOME']) / f'swift-{id(self)}'
        out_dir.mkdir()
        binary = out_dir / 'aria-checks'
        try:
            build = subprocess.run([swiftc, '-DWISP_REMOTE_ONLY_DEMO', '-DWISP_REMOTE_FULL', '-module-cache-path',
                                    str(out_dir / 'cache'), str(ROOT / 'app/Sources/WispApp/AriaDemoMode.swift'),
                                    str(ROOT / 'tests/AriaDemoModeChecks.swift'), '-o', str(binary)],
                                   capture_output=True, text=True, timeout=600)
        except OSError:
            self.skipTest('swiftc cannot run here (sandboxed)')
        if build.returncode != 0 and 'SDK' in build.stderr:
            self.skipTest('no macOS SDK for swiftc')
        self.assertEqual(build.returncode, 0, build.stderr[-3000:])
        run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60,
                             env={'PATH': '/usr/bin:/bin'})
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('AriaDemoModeChecks passed', run.stdout)
