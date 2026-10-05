import json,unittest
from answer_eval import fake_tool
class FixtureTests(unittest.TestCase):
 def call(self,n,a,s='complete'):return json.loads(fake_tool(n,a,s))
 def test_dates_counts_and_body_visibility(self):
  self.assertEqual(self.call('view_messages',{'period':'today'})['items'],[])
  self.assertEqual(len(self.call('view_messages',{'count':1})['items']),1)
  self.assertTrue(self.call('view_messages',{'count':1})['limit_truncated'])
  self.assertEqual(self.call('view_emails',{'day':'today'})['items'],[])
  self.assertTrue(all('body' not in x for x in self.call('summarize_emails',{})['items']))
  self.assertTrue(all('body' in x for x in self.call('view_emails',{})['items']))
 def test_runtime_invalid_args_and_default_digest_window(self):
  self.assertEqual(self.call('view_messages',{'day':'last week'})['status'],'invalid_arguments')
  self.assertEqual(self.call('summarize_emails',{'invented_filter':True})['status'],'invalid_arguments')
  items=self.call('summarize_messages',{})['items']
  self.assertTrue(all(x['time']>='2026-10-02T00:55' for x in items))
  self.assertTrue(any(x['person']=='Sam' for x in items))
  partial=self.call('summarize_messages',{},'partial')['items']
  self.assertTrue(all(x['person']!='Sam' for x in partial))
  self.assertEqual(len(self.call('summarize_messages',{'conversation':'Morgan','period':'last week','count':1})['items']),1)
 def test_partial_withholds_real_fixture_rows(self):
  for n in ('view_messages','view_emails'):
   self.assertLess(len(self.call(n,{},'partial')['items']),len(self.call(n,{})['items']))
  a={'period':'this week','calendar_only':True}
  part=self.call('get_upcoming',a,'partial');full=self.call('get_upcoming',a)
  self.assertLess(len(part['items']),len(full['items']))
  self.assertTrue(all(e['account']!='Work' for e in part['items']))
 def test_calendar_filters_and_reminders(self):
  self.assertEqual(self.call('get_upcoming',{'period':'this week','query':'Dentist','calendar_only':True})['items'],[])
  self.assertTrue(any(e['source']=='reminders' for e in self.call('get_upcoming',{'period':'this week'})['items']))
  self.assertTrue(all(e['account']=='Work' for e in self.call('get_upcoming',{'period':'this month','account':'Work'})['items']))
 def test_notes_scope(self):
  self.assertEqual(self.call('search_notes',{'query':'nonexistent'})['items'],[])
  self.assertEqual(self.call('search_notes',{'query':'kitchen remodel','period':'today'})['items'],[])
  self.assertEqual(len(self.call('search_notes',{'query':'kitchen remodel','period':'last week'})['items']),1)
 def test_failure_not_empty_success(self):
  r=self.call('get_upcoming',{},'failed');self.assertEqual(r['status'],'permission_denied');self.assertIsNone(r['items'])
  self.assertEqual(self.call('find_free_time',{})['status'],'unsupported_fixture')
if __name__=='__main__':unittest.main()
