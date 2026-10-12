import copy
import json
from dataclasses import replace
from unittest.mock import patch
import httpx
from tests.test_intel_remote_policy import RemoteFixture, policy, EndpointConfigurationError
from service.inference.omlx_client import OMLXClient

TOOL={'type':'function','function':{'name':'view_messages','parameters':{'type':'object','properties':{}}}}
CALL={'id':'call_1','type':'function','function':{'name':'view_messages','arguments':'{}'}}
TRANSCRIPT=[{'role':'user','content':'synthetic selected request'},{'role':'assistant','content':'','tool_calls':[CALL]},{'role':'tool','tool_call_id':'call_1','content':'synthetic observed native result'}]
class ClientChecks(RemoteFixture):
    def test_constructor_binds_before_credential_resolution(self):
        with patch('service.inference.attributed_transport.CredentialTransport',return_value=httpx.MockTransport(lambda r:httpx.Response(200,json={}))) as transport:
            c=OMLXClient();self.assertEqual(c.target,policy.remote_default_target());self.assertFalse(c.managed)
            self.assertEqual(transport.call_args.args[0],policy.remote_only_profile().base_url)
            self.assertIsNone(transport.call_args.args[1]);self.assertIsNotNone(transport.call_args.kwargs['key_loader'])
            import asyncio;asyncio.run(c.aclose())
        with patch('service.inference.attributed_transport.CredentialTransport') as transport:
            for kw in [{'base_url':'http://127.0.0.1:8000'},{'api_key':'synthetic'},{'target':replace(policy.remote_default_target(),model='other')},{'target':replace(policy.remote_default_target(),endpoint=replace(policy.remote_default_target().endpoint,base_url='https://other.invalid'))}]:
                with self.assertRaises(EndpointConfigurationError):OMLXClient(**kw)
            transport.assert_not_called()
    def test_actual_fit_preserves_tool_transcript_and_bounds_remaining(self):
        with patch('service.inference.attributed_transport.CredentialTransport',return_value=httpx.MockTransport(lambda r:httpx.Response(200,json={}))):
            c=OMLXClient(target=policy.remote_default_target('agent'))
            model,msgs,tools,cap=c._fit_request(c.target.model,copy.deepcopy(TRANSCRIPT),[TOOL],8000,use_remaining_context=True)
            crowded=[{'role':'system','content':'synthetic rules'},{'role':'assistant','content':'x'*20000}]+copy.deepcopy(TRANSCRIPT)
            with self.assertRaises(EndpointConfigurationError):c._fit_request(c.target.model,crowded,[TOOL],4096)
            self.assertEqual(msgs,TRANSCRIPT);self.assertEqual(tools,[TOOL]);self.assertLessEqual(cap,4096)
            payload=c._payload(model,msgs,tools,'auto',None,cap,True)
            self.assertEqual(payload['messages'],TRANSCRIPT)
            with self.assertRaises(EndpointConfigurationError):c._payload(model,msgs,tools,'auto',None,cap,True,chat_template_kwargs={'enable_thinking':False})
            import asyncio;asyncio.run(c.aclose())
    def test_legacy_raw_constructor_behavior_retained(self):
        self.legacy()
        with patch('service.inference.attributed_transport.CredentialTransport',return_value=httpx.MockTransport(lambda r:httpx.Response(200,json={}))):
            c=OMLXClient(base_url='http://127.0.0.1:8000',api_key='synthetic')
            self.assertTrue(c.managed);self.assertIsNone(c.target)
            self.assertEqual(c._payload('legacy',[],None,None,None,8000,True,legacy_flag=True)['legacy_flag'],True)
            import asyncio;asyncio.run(c.aclose())
    def test_actual_client_fragmented_tool_response_mock_transport(self):
        events=[{'choices':[{'delta':{'tool_calls':[{'index':0,'id':'call_1','function':{'name':'view_messages','arguments':'{'}}]},'finish_reason':None}]},{'choices':[{'delta':{'tool_calls':[{'index':0,'function':{'arguments':'}'}}]},'finish_reason':None}]},{'choices':[{'delta':{},'finish_reason':'tool_calls'}]}]
        wire=b''.join(b'data: '+json.dumps(e).encode()+b'\n\n' for e in events)+b'data: [DONE]\n\n'
        seen=[]
        class Wire(httpx.AsyncByteStream):
            async def __aiter__(self):
                for i in range(0,len(wire),17):yield wire[i:i+17]
        def handle(r):seen.append(json.loads(r.content));return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Wire())
        async def run():
            with patch('service.inference.attributed_transport.CredentialTransport',return_value=httpx.MockTransport(handle)):
                c=OMLXClient(target=policy.remote_default_target('agent'))
                try:result=[e async for e in c.stream_events(c.target.model,TRANSCRIPT,tools=[TOOL],max_tokens=3000)]
                finally:await c.aclose()
            self.assertEqual(result[-1]['message']['tool_calls'],[CALL]);self.assertEqual(seen[0]['messages'],TRANSCRIPT)
            from service.config import quarantine
            import os
            from pathlib import Path
            self.assertEqual(quarantine._gate.directory,Path(os.environ['WISP_HOME'])/'.moe')
            self.assertEqual(quarantine._gate.active,set())
        import asyncio;asyncio.run(run())
