"""Remote-inference policy over a fictional example profile.

Synthetic only: run with a fresh WISP_HOME; sockets are forbidden and no
Keychain item is read. The example origin, model and Keychain name are made up.
"""
import copy
from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

if not os.environ.get('WISP_HOME') or Path(os.environ['WISP_HOME']).resolve() == Path.home()/'.moe':
    raise RuntimeError('Use a fresh disposable WISP_HOME for remote-inference synthetic tests')

from service import config
from service.config import quarantine
from service.config.endpoints import EndpointConfigurationError
from service.inference import remote_policy as policy

from tests.fixtures.remote_inference import EXAMPLE, FULL_LIMITS, example_profile  # noqa: F401 — re-exported


class RemoteFixture(unittest.TestCase):
    profile_overrides = {}

    def setUp(self):
        self.profile = {**copy.deepcopy(EXAMPLE), **self.profile_overrides}
        self.cfg = {'inference': {'remote_only': copy.deepcopy(self.profile)}}
        policy.remote_only_profile.cache_clear()
        self.patches = [patch.object(policy, '_FORCED_MODE', '1'),
                        patch.object(config, 'models_config', side_effect=lambda: self.cfg),
                        patch.object(quarantine, '_gate', quarantine.RecoveryGate(home=lambda: Path(os.environ['WISP_HOME']))),
                        patch.object(socket, 'create_connection', side_effect=AssertionError('real socket forbidden')),
                        patch.object(socket.socket, 'connect', side_effect=AssertionError('real socket forbidden'))]
        for p in self.patches:
            p.start()
        self.addCleanup(self.cleanup_profile)

    def cleanup_profile(self):
        for p in reversed(self.patches):
            p.stop()
        policy.remote_only_profile.cache_clear()

    def legacy(self):
        policy.remote_only_profile.cache_clear()
        self.cfg = {}
        policy._FORCED_MODE = None


class PolicyChecks(RemoteFixture):
    def test_no_built_in_endpoint(self):
        # Every endpoint field is required: nothing defaults to a real server.
        with self.assertRaises(TypeError):
            policy.RemoteOnlyProfile()
        source = Path(policy.__file__).read_text()
        for leftover in ('ts.net', 'tail', 'keychain:gx', 'qwen'):
            self.assertNotIn(leftover, source)

    def test_exact_target_and_all_generation_roles(self):
        for role in policy.remote_only_profile().generation_roles:
            t = policy.remote_default_target(role)
            self.assertEqual(t.model, 'example-model')
            self.assertFalse(t.endpoint.managed)
            self.assertEqual((t.endpoint.base_url, t.endpoint.credential_ref, t.endpoint.api_prefix),
                             ('https://inference.example', 'keychain:example', '/v1'))
            self.assertEqual(t.profile, 'remote-only')
            policy.validate_remote_target(t)
        for role in ['embedding', 'reranker', 'research', 'vision', 'unknown', None, []]:
            with self.assertRaises(EndpointConfigurationError):
                policy.require_generation_role(role)

    def test_snapshot_immutable_after_settings_or_env_change(self):
        p = policy.remote_only_profile()
        self.cfg = {}
        policy._FORCED_MODE = None
        self.assertIs(policy.remote_only_profile(), p)
        with self.assertRaises(FrozenInstanceError):
            p.model_id = 'other'

    def test_legacy_mode_pinned_off(self):
        self.legacy()
        self.assertIsNone(policy.remote_only_profile())
        self.cfg = {'inference': {'remote_only': self.profile}}
        policy._FORCED_MODE = '1'
        self.assertIsNone(policy.remote_only_profile())   # cached snapshot

    def test_config_profile_without_launcher_flag_stays_local(self):
        # A remote_only profile in ~/.moe/config.yaml must not switch a normal
        # Mac to remote inference without WISP_REMOTE_ONLY=1.
        policy.remote_only_profile.cache_clear()
        policy._FORCED_MODE = None
        self.assertIsNone(policy.remote_only_profile())

    def test_flag_without_a_profile_refuses_to_start(self):
        for cfg in ({}, {'inference': {}}, {'inference': {'remote_only': None}},
                    {'inference': {'remote_only': 'https://inference.example'}}):
            self.cfg = cfg
            policy.remote_only_profile.cache_clear()
            with self.subTest(cfg=cfg), self.assertRaisesRegex(EndpointConfigurationError, 'remote_only'):
                policy.remote_only_profile()

    def test_incomplete_unknown_or_malformed_profile_fails_closed(self):
        bad = [('enabled', False), ('model_id', ''), ('model_id', 'has space'), ('model_id', 7),
               ('base_url', 'http://inference.example'), ('base_url', 'https://inference.example/'),
               ('base_url', 'https://inference.example/v1'), ('base_url', 'https://user:pw@inference.example'),
               ('base_url', 'https://inference.example?x=1'), ('base_url', ''),
               ('base_url', ' https://inference.example'), ('base_url', 'https://inference.example '),
               ('base_url', 'https://inference.example\n'), ('base_url', '\nhttps://inference.example'),
               ('base_url', 'https://inference.example\t'), ('base_url', 'https://infer ence.example'),
               ('base_url', 'https://inference.example\x00'), ('base_url', 'https://inference.example\x7f'),
               ('base_url', 'https://inference.exämple'), ('base_url', 'https://inference.example\u200b'),
               ('base_url', 'https://inference.example\u00a0'),
               ('credential_ref', 'env:FAKE'), ('credential_ref', 'keychain:'), ('credential_ref', 'none'),
               ('credential_ref', 'local_omlx'), ('max_tokens', True), ('max_tokens', 9000),
               ('context_window', 100), ('aggregate_bytes', 200000), ('tools', 0), ('tools', 1000),
               ('messages', '32'), ('response_bytes', 1.5), ('api_prefix', 'v1'), ('api_prefix', '/v1/'),
               ('readiness_timeout', 0), ('readiness_timeout', 31), ('readiness_timeout', True),
               ('api_key', 'secret'), ('token', 'secret'), ('headers', {}), ('generation_roles', ['agent'])]
        for key, val in bad:
            raw = copy.deepcopy(self.profile)
            raw[key] = val
            self.cfg = {'inference': {'remote_only': raw}}
            policy.remote_only_profile.cache_clear()
            with self.subTest(key=key, val=val), self.assertRaises(EndpointConfigurationError):
                policy.remote_only_profile()
        for missing in sorted(policy.REQUIRED_KEYS):
            raw = {k: v for k, v in self.profile.items() if k != missing}
            self.cfg = {'inference': {'remote_only': raw}}
            policy.remote_only_profile.cache_clear()
            with self.subTest(missing=missing), self.assertRaisesRegex(EndpointConfigurationError, missing):
                policy.remote_only_profile()
        policy.remote_only_profile.cache_clear()
        policy._FORCED_MODE = '0'
        with self.assertRaises(EndpointConfigurationError):
            policy.remote_only_profile()

    def test_optional_fields(self):
        p = example_profile(api_prefix='/api/v1', readiness_timeout=10, enabled=True)
        self.assertEqual((p.api_prefix, p.readiness_timeout), ('/api/v1', 10.0))
        self.assertEqual(policy.remote_only_profile().readiness_timeout, 5.0)

    def test_output_and_utf8_schema_wire_budget(self):
        p = policy.remote_only_profile()
        self.assertEqual(p.fit_output(8000), 4096)
        for v in [True, False, 0, -1, 1.2]:
            with self.assertRaises(EndpointConfigurationError):
                p.fit_output(v)
        body = {'model': p.model_id, 'messages': [{'role': 'user', 'content': 'synthetic'}], 'stream': True, 'max_tokens': 3000}
        p.validate_payload(body)
        for extra in [{'chat_template_kwargs': {}}, {'model': 'other-model'},
                      {'messages': [{'role': 'user', 'content': '😀' * 6100}]}, {'temperature': 10**10000},
                      {'max_tokens': True}, {'messages': [{'role': 'user', 'content': [{}]}]}]:
            with self.assertRaises(EndpointConfigurationError):
                p.validate_payload({**body, **extra})

    def test_tool_exclusions_are_exactly_the_local_runtime_tools(self):
        self.assertEqual(policy._REMOTE_EXCLUDED, {'create_tool', 'use_skill', 'wisp_skills', 'wisp_mcp'})
        for n in [*policy._REMOTE_EXCLUDED, 'mcp_anything', 'not_a_registered_tool', '', None, []]:
            self.assertFalse(policy.remote_tool_allowed(n), n)
        self.assertFalse(hasattr(policy, 'remote_read_only_mode'))
        self.assertFalse(hasattr(policy, 'remote_full_mode'))
