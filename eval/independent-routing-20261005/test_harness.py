import unittest,asyncio
from unittest.mock import patch,AsyncMock
from run_eval import grade,matches,current,runtime_validation
from intent import compile_intent
from service.tools import REGISTRY
from service.tools.registry import _validate_args
from service.tools.timeranges import resolve_span
from run_eval import ANCHOR
class HarnessChecks(unittest.TestCase):
 def case(self):return {'gold':{'tools':['send_email'],'args':{'send_email':{'to':'pat@example.invalid','subject':'Lunch','body':'See you'}},'forbidden':[],'absent':{}}}
 def test_duplicate_effect_fails(self):
  call={'name':'send_email','arguments':{'to':'pat@example.invalid','subject':'Lunch','body':'See you'}}
  self.assertTrue(grade(self.case(),[call])['strict'])
  self.assertFalse(grade(self.case(),[call,call])['strict'])
 def test_timezone_correctness(self):
  e={'iso':'2026-10-09T12:00'}
  self.assertTrue(matches('2026-10-09T12:00',e,'when_iso'))
  self.assertTrue(matches('2026-10-09T12:00-07:00',e,'when_iso'))
  self.assertFalse(matches('2026-10-09T12:00Z',e,'when_iso'))
 def test_shortcut_precedes_router(self):
  with patch('run_eval.compile_read',return_value=([], 'already shown')),patch('run_eval.router.route',new=AsyncMock(side_effect=AssertionError('must not route'))):
   r=asyncio.run(current({'prompt':'here please'}));self.assertEqual(r['direct'],[])
 def test_period_equivalence_not_rolling(self):
  c={'gold':{'tools':['get_upcoming'],'args':{'get_upcoming':{'period':'this week'}},'forbidden':[],'absent':{}}}
  self.assertTrue(grade(c,[{'name':'get_upcoming','arguments':{'period':'2026-10-05 to 2026-10-11'}}])['strict'])
  self.assertFalse(grade(c,[{'name':'get_upcoming','arguments':{'days':7}}])['strict'])
 def test_tool_specific_dates(self):
  self.assertIsNotNone(runtime_validation('summarize_emails',{'day':'last week'}))
  self.assertIsNotNone(runtime_validation('find_free_time',{'period':'2026-10-06 to 2026-10-06','minutes':60}))
  self.assertIsNone(runtime_validation('find_free_time',{'period':'2026-10-06','minutes':60}))
 def test_compile_read_only(self):
  f=lambda x:compile_intent(x,lambda n,a:_validate_args(REGISTRY[n],a),lambda p:resolve_span(p,now=ANCHOR))
  self.assertIsNone(f({'domain':'email','operation':'send','query':'body'}))
  self.assertIsNone(f({'domain':'calendar','operation':'overview','period':'this weekend'}))
  self.assertEqual(f({'domain':'calendar','operation':'overview','period':'this week'}),[{'name':'get_upcoming','arguments':{'period':'this week'}}])
if __name__=='__main__':unittest.main()
