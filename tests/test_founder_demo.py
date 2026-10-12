"""Essential fixture/dispatcher checks; fake native collaborators, fresh home."""
import asyncio
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import time
import unittest
from dataclasses import asdict
from unittest.mock import patch, Mock

if not os.environ.get('WISP_HOME') or Path(os.environ['WISP_HOME']) == Path.home()/'.moe':
    raise RuntimeError('Use a fresh disposable WISP_HOME')
from service import config
from service.inference import remote_policy
from service.demo import founder
from service.tools import notes_tools, imessage_tools, assistant_tools, email_tools
from service.tools.registry import REGISTRY, run_tool
from service.assistant import sync_status, brief

class FounderDemo(unittest.TestCase):
    def setUp(self):
        cfg={'inference':{'remote_only':asdict(remote_policy.RemoteOnlyProfile())}}
        founder.enabled.cache_clear();founder._wire.cache_clear();remote_policy.remote_only_profile.cache_clear()
        self.patches=[patch.object(founder,'_FLAG','1'),patch.object(founder,'_REMOTE_FLAG','1'),
                      patch.object(remote_policy,'_FORCED_MODE','1'),patch.object(config,'models_config',return_value=cfg),
                      patch.object(socket,'create_connection',side_effect=AssertionError('network forbidden')),
                      patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden')),
                      patch.object(notes_tools.cache_store,'save',side_effect=AssertionError('cache write forbidden'))]
        for p in self.patches:p.start()
        self.addCleanup(self.cleanup)
    def cleanup(self):
        for p in reversed(self.patches):p.stop()
        founder.enabled.cache_clear();founder._wire.cache_clear();remote_policy.remote_only_profile.cache_clear()
    def test_exact_resource_and_gate(self):
        path=Path(founder.__file__).with_name('founder-demo.json')
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),'495216937fd54249713b5126dbaafba9116d3d259016dbcc9670d69f11fc8ee1')
        data=founder.data();self.assertEqual([len(data[k]) for k in ['notes','reminders','contacts']],[12,22,14])
        data['contacts'][0]['given_name']='mutated';self.assertEqual(founder.data()['contacts'][0]['given_name'],'Aria')
        founder.enabled.cache_clear();founder._REMOTE_FLAG=None
        with self.assertRaises(ValueError):founder.enabled()
        founder._REMOTE_FLAG='1';founder._FLAG=None;founder.enabled.cache_clear();self.assertFalse(founder.enabled())
        for name in ['search_notes','lookup_contact','list_contacts']:self.assertFalse(remote_policy.remote_demo_tool_allowed(name))
    def test_actual_note_dispatch_and_native_cache_isolation(self):
        with patch.object(notes_tools,'_notes','native private poison'),patch.object(notes_tools,'_available',False):
            result=asyncio.run(run_tool(REGISTRY['search_notes'],{'query':'runway','count':2}))
            self.assertIn('FICTIONAL FOUNDER DEMO',result);self.assertIn('840',result);self.assertNotIn('native private poison',result)
            self.assertTrue(all('native private poison' not in r['body'] for r in notes_tools._parse()))
            self.assertEqual(notes_tools.raw_notes_snapshot(byte_limit=1).records,())
            self.assertEqual(notes_tools.raw_notes_snapshot().origin,'founder_demo')
            notes_tools.cache_notes('replacement native cache')
            self.assertEqual(notes_tools._notes,'native private poison')
    def test_actual_contact_dispatch_never_mixes_real_messages_names(self):
        with patch.object(imessage_tools,'_contacts',{'2025550100':'Real contact poison'}),patch.object(imessage_tools,'_name_handles',{'real private name':['real@example.net']}):
            result=asyncio.run(run_tool(REGISTRY['lookup_contact'],{'name':'Aria Solano'}))
            self.assertIn('aria.solano@example.com',result);self.assertIn('FICTIONAL',result);self.assertNotIn('poison',result)
            names=asyncio.run(run_tool(REGISTRY['list_contacts'],{}));self.assertIn('14 fictional',names)
            self.assertEqual(imessage_tools.resolve_contact('2025550100: real message'),'2025550100: real message')
            self.assertFalse(imessage_tools.apply_contacts_sync({'contacts':{'bad':'native'}}))
            imessage_tools.cache_contacts({'bad':'native'})
            self.assertEqual(imessage_tools._contacts,{'2025550100':'Real contact poison'})
    def test_reminder_dispatch_relative_dates_and_no_native_wait(self):
        result=asyncio.run(run_tool(REGISTRY['search_reminders'],{'query':'Priya','scope':'all'}))
        self.assertIn('FICTIONAL',result)
        now=founder.datetime(2026,10,10,12,tzinfo=founder.ZONE).timestamp()
        rows=assistant_tools.reminders_matching('today',now=now)
        self.assertTrue(rows);self.assertTrue(all(founder._anchor(r['when_ts']).date()==founder._anchor(now).date() for r in rows))
        with patch('service.assistant.hub.hub.publish',side_effect=AssertionError('native refresh forbidden')):
            readiness=asyncio.run(sync_status._ensure_sources(('notes','reminders')))
        self.assertFalse(readiness['syncing']);self.assertTrue(readiness['reminders_fresh'])
        self.assertTrue(all(s['demo'] for s in readiness['sources']))
    def test_calendar_and_daily_reminders_use_fixture_not_real_store(self):
        now=founder.datetime(2026,10,10,12,tzinfo=founder.ZONE).timestamp()
        event={'id':'real-calendar','source':'calendar','kind':'event','title':'Synthetic current Calendar','when_ts':now+3600,'end_ts':now+7200,'account':'Current Intel Calendar'}
        poison={'source':'reminders','kind':'reminder','title':'Native reminder poison','when_ts':now}
        with patch.object(brief.assistant_store,'calendar_events_overlapping',return_value=[event]),patch.object(brief.assistant_store,'upcoming',return_value=[poison]),patch.object(sync_status,'_scheduled_source',return_value={'id':'calendar','state':'ready','label':'Calendar'}):
            agenda=brief._agenda(now);section=brief._schedule_section(now)
        self.assertEqual(agenda['events'],[event]);self.assertTrue(agenda['reminders']);self.assertNotIn('poison',section);self.assertIn('FICTIONAL',section)
    def test_ten_real_tool_schemas_fit_unchanged_gateway_bounds(self):
        names=remote_policy._READ_TOOLS|{'search_notes','lookup_contact','list_contacts'}
        schemas=[REGISTRY[name].schema() for name in sorted(names)]
        request={'model':'qwen3.6:35b','stream':True,'max_tokens':4096,'tools':schemas,'tool_choice':'auto','messages':[{'role':'user','content':'Synthetic founder workflow request'}]}
        remote_policy.remote_only_profile().validate_payload(request)
        self.assertLessEqual(len(json.dumps({'messages':request['messages'],'tools':schemas},ensure_ascii=False,separators=(',',':')).encode()),24000)
        self.assertTrue(all(remote_policy.remote_demo_tool_allowed(n) for n in names))
        self.assertFalse(remote_policy.remote_demo_tool_allowed('send_message'));self.assertFalse(remote_policy.remote_demo_tool_allowed('create_note'))
    def test_reminders_native_sync_denied_before_database_write(self):
        tree=ast.parse((Path(__file__).parents[1]/'service/main.py').read_text())
        n=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='assistant_sync_calendar');n.decorator_list=[]
        mod=ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),n],type_ignores=[]))
        ns={};exec(compile(mod,'isolated-main','exec'),ns)
        result=asyncio.run(ns['assistant_sync_calendar']({'source':'reminders','events':[{'title':'native poison'}]}))
        self.assertTrue(result['ignored_native_sync']);self.assertEqual(result['synced'],0)

if __name__=='__main__':unittest.main(verbosity=2)
