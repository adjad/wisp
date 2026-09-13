"""Synthetic arrival contracts: never loads an installed service or network tool."""
import copy
import importlib.util
import json
import contextlib
import io
import plistlib
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('arrival', ROOT / 'infra/mac-mini/arrival.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


def fixture():
    binding = dict(source_sha256='a' * 64, artifact_sha256='b' * 64,
                   model_sha256='e' * 64, resource_contract_sha256='f' * 64,
                   node_id='nSynthetic', task_id='ship-synthetic', account='wisp')
    plan = a.plan(binding)
    approval = dict(schema_version=1, plan_sha256=a.digest(plan), binding=binding,
                    nonce='synthetic-nonce', issued_at=100, expires_at=200,
                    gates={g: dict(status='PASS', evidence_sha256='c' * 64) for g in a.GATES})
    return plan, approval


class Adapter:
    qualification_sha256 = 'd' * 64

    def __init__(self, fail=None, restore_fail=False):
        self.fail = fail
        self.restore_fail = restore_fail
        self.events = []
        self.nonces = set()

    def claim(self, nonce, digest):
        if nonce in self.nonces:
            return False
        self.nonces.add(nonce)
        return True

    def preflight(self, plan):
        return dict(schema_version=1, binding=plan['binding'], observed_at=150, uid=501,
                    administrator=True, hardware_qualified=True, model_sha256='e' * 64,
                    resource_contract_sha256='f' * 64, resource_qualified=True,
                    concurrency=1, credential_authenticated=True, firewall_enabled=True,
                    inbound_exceptions=[], primary_only_network=True, funnel=False,
                    loopback_only=True)

    def capture(self, action):
        return ('prior', action)

    def perform(self, action, plan):
        self.events.append(('perform', action))
        if action == self.fail:
            raise RuntimeError('sensitive value must never be returned')

    def verify(self, action, plan):
        return True

    def restore(self, action, receipt):
        assert receipt == ('prior', action)
        self.events.append(('restore', action))
        if self.restore_fail:
            raise RuntimeError('sensitive value')

    def verify_restored(self, action, receipt):
        return True


class ArrivalTests(unittest.TestCase):
    def run_apply(self, adapter=None, plan=None, approval=None, **kwargs):
        p, ap = fixture()
        p = plan if plan is not None else p
        ap = approval if approval is not None else ap
        return a.apply(p, ap, trusted_approval_sha256=a.digest(ap),
                       trusted_adapter_sha256='d' * 64, adapter=adapter,
                       authorize=True, now=150, **kwargs)

    def test_order_and_disabled_migration(self):
        adapter = Adapter()
        result = self.run_apply(adapter)
        self.assertEqual(adapter.events, [('perform', x) for x in a.ACTIONS])
        self.assertEqual(result['role_migration'], 'disabled')
        p, _ = fixture()
        self.assertEqual(p['routes'], {'443': 'http://127.0.0.1:8765', '8443': 'http://127.0.0.1:8766'})

    def test_each_partial_failure_rolls_back_reverse_including_failed_action(self):
        for i, action in enumerate(a.ACTIONS):
            with self.subTest(action=action):
                adapter = Adapter(fail=action)
                with self.assertRaisesRegex(a.ArrivalError, '^ARRIVAL_REFUSED_ROLLED_BACK$'):
                    self.run_apply(adapter)
                self.assertEqual(adapter.events, [('perform', x) for x in a.ACTIONS[:i+1]] +
                                 [('restore', x) for x in reversed(a.ACTIONS[:i+1])])

    def test_inconclusive_rollback_fixed_failure(self):
        with self.assertRaisesRegex(a.ArrivalError, '^ARRIVAL_RECOVERY_REQUIRED$'):
            self.run_apply(Adapter(fail=a.ACTIONS[-1], restore_fail=True))

    def test_interruptions_restore_every_partial_action_before_reraising(self):
        for kind in (KeyboardInterrupt, SystemExit):
            for index, action in enumerate(a.ACTIONS):
                adapter = Adapter()
                interruption = kind('synthetic interruption')
                def perform(current, plan):
                    adapter.events.append(('perform', current))
                    if current == action:
                        raise interruption
                adapter.perform = perform
                with self.assertRaises(kind) as caught:
                    self.run_apply(adapter)
                self.assertIs(caught.exception, interruption)
                self.assertEqual(adapter.events, [('perform', x) for x in a.ACTIONS[:index+1]] +
                                 [('restore', x) for x in reversed(a.ACTIONS[:index+1])])

    def test_restore_interruptions_continue_best_effort_and_require_recovery(self):
        for kind in (KeyboardInterrupt, SystemExit):
            adapter = Adapter(fail=a.ACTIONS[-1])
            def restore(action, receipt):
                adapter.events.append(('restore', action))
                raise kind('synthetic restore interruption')
            adapter.restore = restore
            with self.assertRaisesRegex(a.ArrivalError, '^ARRIVAL_RECOVERY_REQUIRED$'):
                self.run_apply(adapter)
            self.assertEqual([action for operation, action in adapter.events if operation == 'restore'],
                             list(reversed(a.ACTIONS)))

    def test_replay_has_no_second_mutation(self):
        adapter = Adapter()
        self.run_apply(adapter)
        events = list(adapter.events)
        with self.assertRaises(a.ArrivalError):
            self.run_apply(adapter)
        self.assertEqual(events, adapter.events)

    def test_all_gates_mandatory_and_bound(self):
        for gate in a.GATES:
            p, ap = fixture()
            ap['gates'][gate]['status'] = 'PENDING'
            adapter = Adapter()
            with self.assertRaises(a.ArrivalError):
                self.run_apply(adapter, p, ap)
            self.assertEqual(adapter.events, [])
        for key in ('source_sha256', 'artifact_sha256', 'node_id', 'task_id', 'account'):
            p, ap = fixture()
            ap['binding'] = dict(ap['binding'], **{key: 'substitute'})
            with self.assertRaises(a.ArrivalError):
                self.run_apply(Adapter(), p, ap)

    def test_no_self_approval_or_default_live_adapter(self):
        p, ap = fixture()
        with self.assertRaisesRegex(a.ArrivalError, 'UNTRUSTED_APPROVAL'):
            a.apply(p, ap, trusted_approval_sha256='0'*64, authorize=True, adapter=Adapter())
        with self.assertRaisesRegex(a.ArrivalError, 'QUALIFIED_ADAPTER_REQUIRED'):
            self.run_apply()
        with self.assertRaisesRegex(a.ArrivalError, 'EXPLICIT_AUTHORIZATION_REQUIRED'):
            a.apply(p, ap, trusted_approval_sha256=a.digest(ap), now=150)

    def test_expiration_downgrade_extra_fields_and_broad_network(self):
        for changes in ({'expires_at': 150}, {'issued_at': 151}, {'schema_version': 0},
                        {'secret': 'never accepted'}, {'expires_at': 4001}):
            p, ap = fixture()
            ap.update(changes)
            with self.assertRaises(a.ArrivalError):
                self.run_apply(Adapter(), p, ap)
        for changes in ({'funnel': True}, {'network_scope': '*'}, {'actions': ['arbitrary-command']}):
            p, ap = fixture()
            p.update(changes)
            ap['plan_sha256'] = a.digest(p)
            with self.assertRaises(a.ArrivalError):
                self.run_apply(Adapter(), p, ap)

    def test_strict_fresh_nonroot_admin_and_resources(self):
        p, _ = fixture()
        for changes in ({'uid': 0}, {'uid': True}, {'administrator': False},
                        {'administrator': 'true'}, {'observed_at': 89}, {'observed_at': 151},
                        {'concurrency': 2}, {'funnel': True}, {'inbound_exceptions': ['omlx']},
                        {'loopback_only': False}, {'credential_authenticated': False},
                        {'firewall_enabled': False}, {'resource_contract_sha256': ''}):
            with self.subTest(changes=changes):
                evidence = Adapter().preflight(p)
                evidence.update(changes)
                with self.assertRaises(a.ArrivalError):
                    a.validate_preflight(evidence, p['binding'], 150)

    def test_template_is_disabled_secret_free_and_exact(self):
        template = json.loads((ROOT / 'infra/mac-mini/templates/omlx-v1.json').read_text())
        prepared = a.prepare_omlx(template, version='1.2.3', artifact_sha256='a'*64,
                                 verified_artifact_sha256='a'*64)
        self.assertTrue(prepared['supervision']['disabled'])
        self.assertFalse(prepared['supervision']['run_at_load'])
        self.assertFalse(prepared['supervision']['keep_alive'])
        for section, key, value in [('config', 'host', '0.0.0.0'), ('config', 'api_key', 'secret'),
                                    ('supervision', 'disabled', False), ('install', 'automatic_install', True)]:
            changed = copy.deepcopy(template)
            changed[section][key] = value
            with self.assertRaises(a.ArrivalError):
                a.validate_template(changed)
        with self.assertRaises(a.ArrivalError):
            a.prepare_omlx(template, version='1.2.3', artifact_sha256='a'*64,
                           verified_artifact_sha256='b'*64)


    def test_rendered_candidates_disabled_loopback_no_credentials(self):
        template = json.loads((ROOT / 'infra/mac-mini/templates/omlx-v1.json').read_text())
        prepared = a.prepare_omlx(template, version='1.2.3', artifact_sha256='a'*64,
                                 verified_artifact_sha256='a'*64)
        rendered = a.render_omlx(prepared, executable='/Users/wisp/Library/Application Support/Wisp/omlx/1.2.3/bin/omlx',
                                 executable_sha256='b'*64, verified_executable_sha256='b'*64)
        launch = plistlib.loads(rendered['launchagent_plist'])
        self.assertEqual(launch['ProgramArguments'][1:], ['serve', '--host', '127.0.0.1', '--port', '8000'])
        self.assertTrue(launch['Disabled'])
        self.assertFalse(launch['RunAtLoad'])
        self.assertFalse(launch['KeepAlive'])
        self.assertNotIn('EnvironmentVariables', launch)
        self.assertNotIn('api-key', rendered['launchagent_plist'].decode())
        config = json.loads(rendered['config_json'])
        self.assertTrue(config['requires_live_qualification'])
        with self.assertRaises(a.ArrivalError):
            a.render_omlx(prepared, executable='/tmp/untrusted', executable_sha256='b'*64,
                          verified_executable_sha256='b'*64)

    def test_cli_dry_run_and_redacted_bad_input(self):
        p, _ = fixture()
        with tempfile.TemporaryDirectory() as temp:
            binding = Path(temp) / 'binding.json'
            binding.write_text(json.dumps(p['binding']))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = a.main(['--binding', str(binding), '--dry-run'])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue()), p)
            binding.write_text('secret input invalid JSON')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = a.main(['--binding', str(binding), '--apply'])
            self.assertEqual(result, 2)
            self.assertNotIn('secret', output.getvalue())


def integrated_fixture():
    from mini.resources import POLICY, GB
    configuration = {**POLICY, "models": [{"model_id": "synthetic-model", "revision": "a"*40,
        "tokenizer_revision": "b"*40, "runtime_revision": "c"*40, "profile_sha256": "d"*64,
        "quantization_bits": 4, "context_tokens": 16384, "qualified_contexts": [8192, 16384],
        "request_memory_bytes": GB, "request_paged_bytes": GB}]}
    plan, approval = fixture()
    binding = plan['binding']
    binding.update(model_sha256=a.digest(configuration['models']), resource_contract_sha256=a.digest(configuration))
    plan = a.plan(binding)
    approval.update(binding=binding, plan_sha256=a.digest(plan))
    preflight = Adapter().preflight(plan)
    preflight.update(model_sha256=binding['model_sha256'], resource_contract_sha256=binding['resource_contract_sha256'])
    evidence = dict(schema_version=1, mode='synthetic', preflight=preflight,
        identity=dict(node_id=binding['node_id'], account=binding['account'], host_key_sha256='a'*64,
                      tailnet_policy_sha256='b'*64, primary_ip='100.94.211.115', device_approved=True,
                      magic_dns=True, https=True, serve=plan['routes']),
        artifact=dict(source_sha256=binding['source_sha256'], archive_sha256=binding['artifact_sha256'],
                      signature_verified=True, publisher_key_sha256='a'*64), models=configuration['models'],
        telemetry=dict(schema_version=1, contract_sha256=a.digest(configuration), sampled_at_ns=100,
                       memory_used_bytes=10*GB, hot_cache_used_bytes=GB, paged_kv_used_bytes=GB,
                       engine_limits_enforced=True),
        storage=dict(devices={k: 1 for k in ('model','cache','telemetry','state','backup')}, free_bytes=200*GB,
                     reserved_bytes=50*GB, growth_bytes=20*GB, quota_enforced=True, sole_backend=True),
        backup=dict(schema_version=2, sqlite_online=True, integrity_verified=True, restore_new_directory=True,
                    cursor_identity='rotate', restore_test_sha256='a'*64),
        qualification=[dict(model_id='synthetic-model', profile_sha256='d'*64,
                            steps=[dict(context_tokens=n, completed_at=t, passed=True, report_sha256='e'*64)
                                   for n,t in ((8192,100),(16384,120))])], jobs_enabled=False, providers_enabled=False)
    return plan, approval, configuration, evidence


class IntegratedArrivalTests(unittest.TestCase):
    def validate(self, config=None, evidence=None):
        p, _, c, e = integrated_fixture()
        c = c if config is None else config
        e = e if evidence is None else evidence
        return a.prepare_integrated(p, c, e, trusted_evidence_sha256=a.digest(e), now=150, monotonic_ns=100)

    def test_prepared_adapter_rehearsal_never_authorizes_live_actions(self):
        p, approval, c, e = integrated_fixture()
        adapter = a.PreparedArrivalAdapter(p, c, e, trusted_evidence_sha256=a.digest(e), now=150, monotonic_ns=100)
        args = dict(trusted_approval_sha256=a.digest(approval), adapter=adapter,
                    trusted_adapter_sha256=adapter.qualification_sha256, authorize=True, now=150)
        with self.assertRaisesRegex(a.ArrivalError, 'MODE_MISMATCH'):
            a.apply(p, approval, **args)
        self.assertEqual(adapter.actions, [])
        result = a.apply(p, approval, simulate=True, **args)
        self.assertEqual(result['status'], 'ARRIVAL_SIMULATED')
        self.assertEqual(adapter.actions, list(a.ACTIONS))
        with self.assertRaises(a.ArrivalError):
            a.apply(p, approval, simulate=True, **args)
        self.assertFalse(adapter.validate()['live_apply'])

    def test_every_evidence_section_is_required(self):
        _, _, _, evidence = integrated_fixture()
        for key in evidence:
            changed = copy.deepcopy(evidence)
            del changed[key]
            with self.subTest(key=key), self.assertRaises(a.ArrivalError):
                self.validate(evidence=changed)

    def test_resource_and_model_policy_cannot_be_weakened(self):
        from mini.resources import POLICY
        _, _, config, _ = integrated_fixture()
        for key in POLICY:
            changed = copy.deepcopy(config)
            changed[key] = None
            with self.subTest(key=key), self.assertRaises(a.ArrivalError):
                self.validate(config=changed)
        for changes in ({'quantization_bits':3}, {'qualified_contexts':[16384]}, {'revision':'main'},
                        {'runtime_revision':'unverified'}, {'request_memory_bytes':0}):
            changed = copy.deepcopy(config)
            changed['models'][0].update(changes)
            with self.assertRaises(a.ArrivalError):
                self.validate(config=changed)

    def test_adversarial_host_capacity_backup_and_qualification(self):
        from mini.resources import GB
        for section, key, value in (
            ('identity','primary_ip','100.1.1.1'), ('identity','account','root'), ('identity','device_approved',False),
            ('artifact','signature_verified',False), ('artifact','archive_sha256','0'*64),
            ('storage','devices',dict(model=1,cache=2,telemetry=1,state=1,backup=1)),
            ('storage','free_bytes',150*GB-1), ('storage','reserved_bytes',50*GB-1),
            ('storage','growth_bytes',151*GB), ('storage','quota_enforced',False), ('storage','sole_backend',False),
            ('telemetry','memory_used_bytes',60*GB), ('telemetry','hot_cache_used_bytes',2*GB+1),
            ('telemetry','paged_kv_used_bytes',20*GB), ('telemetry','sampled_at_ns',101),
            ('telemetry','engine_limits_enforced',False), ('backup','restore_new_directory',False),
            ('backup','cursor_identity','preserve'), ('backup','integrity_verified',False),
            ('preflight','administrator',False), ('preflight','credential_authenticated',False),
            ('preflight','funnel',True)):
            _, _, _, e = integrated_fixture()
            e[section][key] = value
            with self.subTest(section=section,key=key), self.assertRaises(a.ArrivalError):
                self.validate(evidence=e)
        for key,value in (('mode','live'), ('jobs_enabled',True), ('providers_enabled',True)):
            _,_,_,e = integrated_fixture(); e[key]=value
            with self.assertRaises(a.ArrivalError): self.validate(evidence=e)
        _,_,_,e = integrated_fixture()
        e['qualification'][0]['steps'].reverse()
        with self.assertRaises(a.ArrivalError): self.validate(evidence=e)

    def test_mutated_evidence_pin_and_fixture_state_refuse(self):
        p, _, c, e = integrated_fixture()
        with self.assertRaisesRegex(a.ArrivalError, 'UNTRUSTED'):
            a.prepare_integrated(p,c,e,trusted_evidence_sha256='0'*64,now=150,monotonic_ns=100)
        adapter = a.PreparedArrivalAdapter(p,c,e,trusted_evidence_sha256=a.digest(e),now=150,monotonic_ns=100)
        adapter.evidence['storage']['free_bytes'] = 1
        with self.assertRaises(a.ArrivalError): adapter.preflight(p)
        self.assertEqual(adapter.actions, [])

    def test_repository_and_packaged_cli_from_foreign_directory(self):
        import os
        import shutil
        import subprocess
        import sys
        import time
        p, _, c, e = integrated_fixture()
        e['preflight']['observed_at'] = int(time.time())
        e['telemetry']['sampled_at_ns'] = time.monotonic_ns()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name, value in (('binding',p['binding']), ('config',c), ('evidence',e)):
                path = root / (name+'.json')
                path.write_text(json.dumps(value)); path.chmod(0o600)
            packaged = root / 'payload/preparation'
            packaged.mkdir(parents=True)
            shutil.copyfile(ROOT/'infra/mac-mini/arrival.py', packaged/'arrival.py')
            shutil.copytree(ROOT/'mini', root/'payload/runtime/mini', ignore=shutil.ignore_patterns('__pycache__'))
            environment = {k:v for k,v in os.environ.items() if not k.startswith('PYTHON')}
            for script in (ROOT/'infra/mac-mini/arrival.py', packaged/'arrival.py'):
                result = subprocess.run([sys.executable,'-I','-B',str(script),'--binding',str(root/'binding.json'),
                    '--dry-run','--resource-contract',str(root/'config.json'),'--preparation-evidence',str(root/'evidence.json'),
                    '--trusted-evidence-sha256',a.digest(e)],cwd=root,env=environment,capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertFalse(json.loads(result.stdout)['live_apply'])


if __name__ == '__main__':
    unittest.main()
