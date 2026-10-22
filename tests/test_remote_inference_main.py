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

def evaluate_context_keyword(keyword, namespace):
    """Execute the actual /agent keyword expression with synthetic bindings."""
    tree = ast.parse(SOURCE.read_text())
    agent = next(n for n in tree.body
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == 'agent')
    calls = [n for n in ast.walk(agent) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == 'run_agent']
    assert len(calls) == 1
    expression = next(k.value for k in calls[0].keywords if k.arg == keyword)
    code = compile(ast.Expression(body=expression), str(SOURCE), 'eval')
    return eval(code, {'__builtins__': {}, 'bool': bool, 'set': set}, namespace)


class MainChecks(RemoteFixture):
    def test_startup_remote_health_without_local_or_background_and_closes(self):
        fake=SimpleNamespace(health=AsyncMock(return_value={'ok':True}),aclose=AsyncMock())
        q=ModuleType('service.config.quarantine');q.check=Mock()
        async def watch(tasks,close):
            self.assertEqual(len(tasks),1);await asyncio.Event().wait()  # exactly the assistant scheduler
        q.watch=watch
        async def scheduler_run():
            await asyncio.Event().wait()
        identity=ModuleType('service.identity');identity.refuse_sandbox_on_production_port=Mock()
        host=ModuleType('service.browser.host');host.start_from_environment=Mock(return_value=None)
        browser=ModuleType('service.browser');browser.host=host
        bomb=Mock(side_effect=AssertionError('local/background operation forbidden'))
        ns={'asyncio':asyncio,'asynccontextmanager':asynccontextmanager,'remote_only_profile':policy.remote_only_profile,'require_local_models_disabled':policy.require_local_models_disabled,
            'OMLXClient':lambda:fake,'role_to_model':lambda role:'example-model','skills':SimpleNamespace(load=bomb),
            'mcp_manager':SimpleNamespace(start=bomb,stop=bomb),'idle_unloader':SimpleNamespace(run=bomb),
            'assistant_scheduler':SimpleNamespace(run=scheduler_run),'cloud_super_model_enabled':bomb,'start_laya_warmup':bomb,
            'research_manager':SimpleNamespace(store=SimpleNamespace(purge_stale_bodies=Mock())),
            'research_cache':SimpleNamespace(purge_older_than=Mock()),'assistant_store':object(),'is_bundled':lambda:False}
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
    def test_ui_write_handlers_have_no_remote_guard(self):
        names={'assistant_add','assistant_update','assistant_delete','assistant_send_message_draft','assistant_event_claim','approve'}
        tree=ast.parse(SOURCE.read_text())
        found=set()
        for node in tree.body:
            if isinstance(node,ast.AsyncFunctionDef) and node.name in names:
                found.add(node.name)
                self.assertNotIn('remote',ast.unparse(node).lower().replace('remote_only_profile','').replace('remote inference',''),node.name)
        self.assertEqual(found,names)
    def test_current_context_and_no_summary_source_contract(self):
        # Precise source assertions supplement executable client/read tests:
        # automatic prior-history summarization must stay conditional-off.
        tree=ast.parse(SOURCE.read_text())
        agent=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='agent')
        summary=next(n for n in ast.walk(agent) if isinstance(n,ast.If) and any(isinstance(c,ast.Call) and isinstance(c.func,ast.Name) and c.func.id=='maybe_summarize' for c in ast.walk(n)))
        self.assertIn('remote_only_profile() is None',ast.unparse(summary.test))
        calls=[n for n in ast.walk(agent) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='run_agent']
        self.assertEqual(len(calls),1)
        self.assertTrue({'include_memory_context', 'allow_skill_context',
                         'include_identity_context'} <= {k.arg for k in calls[0].keywords})
        # The executable controls below check both the ordinary Mac policy and
        # dry-run short circuit, without depending on expression formatting.
        source=SOURCE.read_text()
        self.assertIn('messages = ([user_msg] if super_model_cloud else _tool_turn_messages(',source)
        self.assertNotIn('remote_read_only_mode',source)

    def test_dry_run_context_guards_short_circuit_saved_policy(self):
        for model_led in (False, True):
            with self.subTest(model_led=model_led):
                bomb = Mock(side_effect=AssertionError('saved policy must not be consulted'))
                namespace = {'test_mode': True, 'model_led_turn': model_led,
                             '_agent_memory_context_allowed': bomb,
                             'remote_only_profile': bomb,
                             # Invalid ordinary inputs prove the guard stops
                             # before inspecting saved-context policy values.
                             'decision': object(), 'workflow_turn': object(),
                             'super_model_cloud': object(), 'schedule_read': object(),
                             'literal_envelope': object()}
                for keyword in ('include_memory_context', 'allow_skill_context',
                                'include_identity_context'):
                    self.assertIs(evaluate_context_keyword(keyword, namespace), False)
                bomb.assert_not_called()

    def test_ordinary_memory_uses_same_policy_for_local_and_remote(self):
        namespace = {}
        selected({'_agent_memory_context_allowed'}, namespace)
        real_policy = namespace['_agent_memory_context_allowed']
        for remote in (False, True):
            for cloud in (False, True):
                for grounded in (False, True):
                    for verified in (False, True):
                        for schedule in (False, True):
                            with self.subTest(remote=remote, cloud=cloud, grounded=grounded,
                                              verified=verified, schedule=schedule):
                                decision = SimpleNamespace(verified_results_only=verified,
                                                           forbidden_tools=frozenset())
                                policy_call = Mock(wraps=real_policy)
                                bindings = {'test_mode': False, 'model_led_turn': False,
                                    'decision': decision, 'super_model_cloud': cloud,
                                    'workflow_turn': SimpleNamespace(decision=object()) if grounded else None,
                                    'schedule_read': schedule,
                                    '_agent_memory_context_allowed': policy_call,
                                    'remote_only_profile': lambda: policy.remote_only_profile() if remote else None}
                                self.assertIs(evaluate_context_keyword('include_memory_context', bindings),
                                              not (cloud or grounded or verified or schedule))
                                policy_call.assert_called_once_with(decision, cloud=cloud,
                                    grounded_workflow=grounded, schedule_read=schedule)

    def test_model_led_memory_keeps_forbidden_recall_and_search_exclusions(self):
        for remote in (False, True):
            for forbidden in (frozenset(), frozenset({'recall'}), frozenset({'search_memory'}),
                              frozenset({'recall', 'search_memory'}), frozenset({'get_upcoming'})):
                with self.subTest(remote=remote, forbidden=forbidden):
                    bomb = Mock(side_effect=AssertionError('legacy policy must not run on model-led path'))
                    bindings = {'test_mode': False, 'model_led_turn': True,
                                'decision': SimpleNamespace(forbidden_tools=forbidden),
                                '_agent_memory_context_allowed': bomb,
                                'remote_only_profile': lambda: policy.remote_only_profile() if remote else None}
                    self.assertIs(evaluate_context_keyword('include_memory_context', bindings),
                                  not bool({'recall', 'search_memory'} & forbidden))
                    bomb.assert_not_called()

    def test_ordinary_skill_and_identity_controls_keep_remote_and_literal_exclusions(self):
        for remote in (False, True):
            for literal in (False, True):
                with self.subTest(remote=remote, literal=literal):
                    profile = policy.remote_only_profile() if remote else None
                    remote_profile = Mock(return_value=profile)
                    bindings = {'test_mode': False, 'remote_only_profile': remote_profile,
                                'literal_envelope': literal}
                    self.assertIs(evaluate_context_keyword('allow_skill_context', bindings),
                                  not remote and not literal)
                    remote_profile.assert_called_once_with()
                    self.assertIs(evaluate_context_keyword('include_identity_context', bindings), True)
