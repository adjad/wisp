"""Execute exact AST-selected main functions against synthetic collaborators.

No main app import, listener, actual state/native model service or fake QA claim.
"""
import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace,ModuleType
from unittest.mock import AsyncMock, Mock, patch
from fastapi import HTTPException
from tests.test_remote_inference_policy import RemoteFixture,policy

SOURCE=Path(__file__).resolve().parents[1]/'service/main.py'
def selected(names,namespace):
    tree=ast.parse(SOURCE.read_text())
    nodes=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)]
    nodes.extend(n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names)
    module=ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[]))
    namespace.setdefault('app',SimpleNamespace(post=lambda *a,**k:lambda f:f,delete=lambda *a,**k:lambda f:f))
    exec(compile(module,str(SOURCE),'exec'),namespace)
    return namespace

class MainChecks(RemoteFixture):
    def test_startup_remote_health_without_local_or_background_and_closes(self):
        fake=SimpleNamespace(health=AsyncMock(return_value={'ok':True}),aclose=AsyncMock())
        q=ModuleType('service.config.quarantine');q.check=Mock()
        async def watch(tasks,close):
            self.assertEqual(tasks,());await asyncio.Event().wait()
        q.watch=watch
        identity=ModuleType('service.identity');identity.refuse_sandbox_on_production_port=Mock()
        host=ModuleType('service.browser.host');host.start_from_environment=Mock(return_value=None)
        browser=ModuleType('service.browser');browser.host=host
        bomb=Mock(side_effect=AssertionError('local/background operation forbidden'))
        ns={'asyncio':asyncio,'asynccontextmanager':asynccontextmanager,'remote_only_profile':policy.remote_only_profile,'remote_full_mode':policy.remote_full_mode,'require_local_models_disabled':policy.require_local_models_disabled,
            'OMLXClient':lambda:fake,'role_to_model':lambda role:'example-model','skills':SimpleNamespace(load=bomb),
            'mcp_manager':SimpleNamespace(start=bomb,stop=bomb),'idle_unloader':SimpleNamespace(run=bomb),
            'assistant_scheduler':SimpleNamespace(run=bomb),'cloud_super_model_enabled':bomb,'start_laya_warmup':bomb,
            'research_manager':SimpleNamespace(store=SimpleNamespace(purge_stale_bodies=Mock())),
            'research_cache':SimpleNamespace(purge_older_than=Mock()),'assistant_store':object()}
        selected({'lifespan','_sync_keep_warm','_prepare_remote_inference'},ns)
        async def run():
            with patch.dict('sys.modules',{'service.identity':identity,'service.config.quarantine':q,'service.browser':browser,'service.browser.host':host}),patch('service.config.quarantine',q):
                with self.assertRaisesRegex(RuntimeError,'synthetic body'):
                    async with ns['lifespan'](None):raise RuntimeError('synthetic body')
        asyncio.run(run());fake.health.assert_awaited_once();fake.aclose.assert_awaited_once();bomb.assert_not_called();q.check.assert_called_once();identity.refuse_sandbox_on_production_port.assert_called_once()
        fake.health.reset_mock();fake.aclose.reset_mock();fake.health.side_effect=RuntimeError('synthetic remote unavailable')
        async def startup_failure():
            with patch.dict('sys.modules',{'service.identity':identity,'service.config.quarantine':q,'service.browser':browser,'service.browser.host':host}),patch('service.config.quarantine',q):
                with self.assertRaisesRegex(RuntimeError,'synthetic remote unavailable'):
                    async with ns['lifespan'](None):self.fail('startup must fail closed')
        asyncio.run(startup_failure());fake.health.assert_awaited_once();fake.aclose.assert_awaited_once();bomb.assert_not_called()
    def test_lifecycle_denied_before_any_cli_or_health(self):
        bomb=AsyncMock(side_effect=AssertionError('local CLI forbidden'))
        ns={'require_local_models_disabled':policy.require_local_models_disabled,'client':SimpleNamespace(managed=True,health=bomb),'_await_omlx':bomb,'_omlx_cli':bomb}
        selected({'ensure_omlx'},ns)
        with self.assertRaises(ValueError):asyncio.run(ns['ensure_omlx']())
        bomb.assert_not_called()
    def test_six_ui_write_handlers_deny_before_body(self):
        names={'assistant_add','assistant_update','assistant_delete','assistant_send_message_draft','assistant_event_claim','approve'}
        ns={'remote_only_profile':policy.remote_only_profile,'remote_read_only_mode':policy.remote_read_only_mode,'HTTPException':HTTPException}
        selected(names,ns)
        async def run():
            for name in names:
                args=('synthetic-id',{}) if name in {'assistant_update','assistant_event_claim'} else ('synthetic-id',) if name in {'assistant_delete','assistant_event_claim'} else ({},)
                with self.subTest(name=name),self.assertRaises(HTTPException) as e:await ns[name](*args)
                self.assertEqual(e.exception.status_code,403)
        asyncio.run(run())
    def test_current_context_and_no_summary_source_contract(self):
        # Precise source assertions supplement executable client/read tests:
        # automatic prior-history summarization must stay conditional-off.
        tree=ast.parse(SOURCE.read_text())
        agent=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='agent')
        summary=next(n for n in ast.walk(agent) if isinstance(n,ast.If) and any(isinstance(c,ast.Call) and isinstance(c.func,ast.Name) and c.func.id=='maybe_summarize' for c in ast.walk(n)))
        self.assertIn('remote_only_profile() is None',ast.unparse(summary.test))
        calls=[n for n in ast.walk(agent) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='run_agent']
        self.assertEqual(len(calls),1)
        kw={k.arg:ast.unparse(k.value) for k in calls[0].keywords}
        # Read-only remote keeps memory context off; the full build (WISP_REMOTE_FULL) follows the Mac path.
        self.assertTrue(kw['include_memory_context'].startswith('not remote_read_only_mode() and'))
        self.assertTrue(kw['allow_skill_context'].startswith('remote_only_profile() is None and'))
        source=SOURCE.read_text()
        self.assertIn('messages = ([user_msg] if super_model_cloud or remote_read_only_mode()',source)
