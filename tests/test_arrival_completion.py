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


if __name__ == '__main__':
    unittest.main()
