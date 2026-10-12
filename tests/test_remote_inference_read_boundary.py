import service.main  # noqa: F401 — registers every built-in tool
import asyncio
from unittest.mock import patch,Mock
from tests.test_remote_inference_policy import RemoteFixture,policy
from service.safety import policy as safety
from service.tools.registry import Tool,run_tool

EXCLUDED=['create_tool','use_skill','wisp_skills','wisp_mcp','mcp_anything',None]

class ReadBoundaryChecks(RemoteFixture):
    def test_denial_of_on_mac_model_tools_survives_full_access_grants_floor_settings(self):
        with patch.object(safety,'_FULL_ACCESS',True),patch.object(safety,'_READ_ONLY',False),patch.object(safety,'_KEEP_FLOOR',False):
            for name in EXCLUDED:
                self.assertEqual(safety.decide('fs_read',{},name).tier,safety.Tier.DENY,name)
    def test_ordinary_tools_are_not_denied_by_the_remote_profile(self):
        # Read/write: sends still confirm, but through the normal policy, not a remote denial.
        with patch.object(safety,'_FULL_ACCESS',False),patch.object(safety,'_READ_ONLY',False):
            d=safety.decide('messages_send',{'to':'+15555550100','text':'hi'},'send_message')
        self.assertIs(d.tier,safety.Tier.CONFIRM)
        self.assertNotIn('on-Mac',d.reason)
    def test_allowed_read_continues_existing_policy_not_auto_allow(self):
        floor=safety.Decision(safety.Tier.DENY,'synthetic existing floor')
        with patch.object(safety,'_hard_deny',return_value=floor),patch.object(safety,'_KEEP_FLOOR',True):
            self.assertIs(safety.decide('fs_read',{},'view_messages'),floor)
    def test_registry_denies_before_args_or_native_function(self):
        native=Mock(side_effect=AssertionError('native side effect forbidden'))
        tool=Tool('use_skill','synthetic',{'type':'object'},'fs_read',native)
        with patch('service.tools.registry._validate_args',side_effect=AssertionError('should deny before args')):
            result=asyncio.run(run_tool(tool,{'text':'synthetic'}))
        self.assertIs(type(result),str);self.assertIn('nothing was executed',result);native.assert_not_called()
    def test_real_read_dispatch_with_fake_reader_and_args_validation(self):
        observed=[]
        async def reader(query):observed.append(query);return 'synthetic Mac-local read'
        tool=Tool('view_messages','synthetic',{'type':'object','properties':{'query':{'type':'string'}},'required':['query']},'fs_read',reader)
        self.assertEqual(asyncio.run(run_tool(tool,{'query':'synthetic'})),'synthetic Mac-local read')
        self.assertEqual(observed,['synthetic'])
        self.assertIn('required',asyncio.run(run_tool(tool,{})).lower())
        self.assertEqual(observed,['synthetic'])
    def test_profile_off_existing_dispatch_unchanged(self):
        self.legacy();native=Mock(return_value='synthetic legacy')
        tool=Tool('send_message','synthetic',{'type':'object','properties':{}},'fs_read',native)
        self.assertEqual(asyncio.run(run_tool(tool,{})),'synthetic legacy');native.assert_called_once()
