import json,unittest
from unittest.mock import patch
import intent_v2
class FakeResponse:
 def __init__(self,value):self.value=value
 def raise_for_status(self):pass
 def json(self):return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(self.value)}}]}
class FakeClient:
 def __init__(self,values):self.values=iter(values);self.http=self;self.requests=[]
 def status(self):return {'models':[{'id':intent_v2.MODEL,'loaded':True}]}
 def post(self,url,**kwargs):self.requests.append(kwargs['json']);return FakeResponse(next(self.values))
class IntentV2Tests(unittest.TestCase):
 def test_rejects_array_unknown_fields_and_boolean_count(self):
  for x in [[],{'domain':'calendar','operation':'overview','send':True},{'domain':'calendar','operation':'overview','count':True}]:
   with self.assertRaises(ValueError):intent_v2.validate_value(x)
 def test_normalizes_period_and_omits_null(self):
  x=intent_v2.validate_value({'domain':'calendar','operation':'overview','period':'next_week','count':None})
  self.assertEqual(x,{'domain':'calendar','operation':'overview','period':'next week'})
 def test_second_invalid_fails_closed(self):
  cli=FakeClient([[],[]]);r=intent_v2.route({'prompt':'My agenda'},cli)
  self.assertTrue(r['invalid_intent']);self.assertEqual(r['calls'],[]);self.assertEqual(r['names'],[]);self.assertEqual(len(cli.requests),2)
 def test_one_repair_retains_original_context_and_compiles(self):
  cli=FakeClient([[],{'domain':'calendar','operation':'overview','period':'next week','calendar_only':True}]);r=intent_v2.route({'prompt':'Then next week, no reminders','context':[{'role':'user','content':'My agenda today'},{'role':'assistant','content':'A meeting. [Tools: get_upcoming]'}]},cli)
  self.assertEqual(r['calls'],[{'name':'get_upcoming','arguments':{'period':'next week','calendar_only':True}}]);self.assertEqual(len(cli.requests),2)
  self.assertIn({'role':'user','content':'Then next week, no reminders'},cli.requests[1]['messages']);self.assertNotIn('[Tools:',str(cli.requests[0]))
 def test_inline_operation_cannot_fall_back_to_message_tools(self):
  cli=FakeClient([{'domain':'messages','operation':'inline','query':'some suggested wording'}]);r=intent_v2.route({'prompt':'Draft wording here'},cli)
  self.assertEqual(r['calls'],[]);self.assertEqual(r['names'],[]);self.assertTrue(r['compiled'])
 def test_invalid_read_period_fails_closed_after_repair(self):
  x={'domain':'calendar','operation':'overview','period':'neverday'};cli=FakeClient([x,x]);r=intent_v2.route({'prompt':'My agenda'},cli)
  self.assertTrue(r['invalid_intent']);self.assertEqual(r['calls'],[]);self.assertEqual(len(cli.requests),2)
 def test_absent_model_refuses_inference(self):
  cli=FakeClient([]);cli.status=lambda:{'models':[]}
  with self.assertRaises(RuntimeError):intent_v2.route({'prompt':'Hi'},cli)
  self.assertEqual(cli.requests,[])
if __name__=='__main__':unittest.main()
