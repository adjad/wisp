"""Scoped synthetic checks. Run all five only with a fresh WISP_HOME."""
import copy
from dataclasses import asdict, FrozenInstanceError
import os
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

if not os.environ.get('WISP_HOME') or Path(os.environ['WISP_HOME']).resolve() == Path.home()/'.moe':
    raise RuntimeError('Use a fresh disposable WISP_HOME for Intel synthetic tests')

from service import config
from service.config import quarantine
from service.config.endpoints import EndpointConfigurationError
from service.inference import remote_policy as policy

class RemoteFixture(unittest.TestCase):
    def setUp(self):
        self.profile=asdict(policy.RemoteOnlyProfile())
        self.profile['generation_roles']=list(self.profile['generation_roles'])
        self.profile['tool_capabilities']=list(self.profile['tool_capabilities'])
        self.cfg={'inference':{'remote_only':copy.deepcopy(self.profile)}}
        policy.remote_only_profile.cache_clear()
        self.patches=[patch.object(policy,'_FORCED_MODE','1'),patch.object(config,'models_config',side_effect=lambda:self.cfg),
                      patch.object(quarantine,'_gate',quarantine.RecoveryGate(home=lambda:Path(os.environ['WISP_HOME']))),
                      patch.object(socket,'create_connection',side_effect=AssertionError('real socket forbidden')),
                      patch.object(socket.socket,'connect',side_effect=AssertionError('real socket forbidden'))]
        for p in self.patches:p.start()
        self.addCleanup(self.cleanup_profile)
    def cleanup_profile(self):
        for p in reversed(self.patches):p.stop()
        policy.remote_only_profile.cache_clear()
    def legacy(self):
        policy.remote_only_profile.cache_clear();self.cfg={}
        policy._FORCED_MODE=None

class PolicyChecks(RemoteFixture):
    def test_exact_target_and_all_generation_roles(self):
        for role in self.profile['generation_roles']:
            t=policy.remote_default_target(role)
            self.assertEqual(t.model,'qwen3.6:35b');self.assertFalse(t.endpoint.managed)
            self.assertEqual(t.endpoint.credential_ref,'keychain:gx10demo')
            policy.validate_remote_target(t)
        for role in ['embedding','reranker','research','vision','unknown',None,[]]:
            with self.assertRaises(EndpointConfigurationError):policy.require_generation_role(role)
    def test_snapshot_immutable_after_settings_or_env_change(self):
        p=policy.remote_only_profile();self.cfg={};policy._FORCED_MODE=None
        self.assertIs(policy.remote_only_profile(),p)
        with self.assertRaises(FrozenInstanceError):p.model_id='other'
    def test_legacy_mode_pinned_off(self):
        self.legacy();self.assertIsNone(policy.remote_only_profile())
        self.cfg={'inference':{'remote_only':self.profile}};policy._FORCED_MODE='1'
        self.assertIsNone(policy.remote_only_profile())
    def test_config_profile_without_launcher_flag_stays_local(self):
        # A remote_only profile in packaged defaults or ~/.moe/config.yaml must not
        # switch a normal Mac to remote inference without WISP_REMOTE_ONLY=1.
        policy.remote_only_profile.cache_clear();policy._FORCED_MODE=None
        self.assertIsNone(policy.remote_only_profile())
    def test_malformed_profile_or_force_fails_closed(self):
        for key,val in [('enabled',False),('model_id','other'),('base_url','https://other.invalid'),('max_tokens',True),('credential_ref','env:FAKE'),('generation_roles',['agent'])]:
            self.cfg={'inference':{'remote_only':copy.deepcopy(self.profile)}};self.cfg['inference']['remote_only'][key]=val
            policy.remote_only_profile.cache_clear()
            with self.assertRaises(EndpointConfigurationError):policy.remote_only_profile()
        policy.remote_only_profile.cache_clear();self.cfg={}
        with self.assertRaises(EndpointConfigurationError):policy.remote_only_profile()
        policy._FORCED_MODE='0'
        with self.assertRaises(EndpointConfigurationError):policy.remote_only_profile()
    def test_output_and_utf8_schema_wire_budget(self):
        p=policy.remote_only_profile();self.assertEqual(p.fit_output(8000),4096)
        for v in [True,False,0,-1,1.2]:
            with self.assertRaises(EndpointConfigurationError):p.fit_output(v)
        body={'model':p.model_id,'messages':[{'role':'user','content':'synthetic'}],'stream':True,'max_tokens':3000}
        p.validate_payload(body)
        for extra in [{'chat_template_kwargs':{}},{'messages':[{'role':'user','content':'😀'*6100}]},{'temperature':10**10000},{'max_tokens':True},{'messages':[{'role':'user','content':[{}]}]}]:
            with self.assertRaises(EndpointConfigurationError):p.validate_payload({**body,**extra})
    def test_exact_seven_tool_names(self):
        names={'get_upcoming','get_past_events','search_reminders','view_emails','summarize_emails','view_messages','summarize_messages'}
        self.assertEqual(policy._READ_TOOLS,names)
        for n in names:self.assertTrue(policy.remote_demo_tool_allowed(n))
        for n in ['lookup_contact','list_contacts','send_message','read_file','get_upcoming_alias',None,[]]:self.assertFalse(policy.remote_demo_tool_allowed(n))
