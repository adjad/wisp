import json,hashlib,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import analyze_results as a
class AnalysisTests(unittest.TestCase):
 def fixture(self,p):
  corpus=[{'id':'case','family':'family','split':'test','gold':{'domains':['calendar']}}]
  raw=json.dumps(corpus);(p/'corpus.json').write_text(raw)
  (p/'heldout.jsonl.manifest.json').write_text(json.dumps({'corpus_sha256':hashlib.sha256(raw.encode()).hexdigest(),'source_sha256':{}}))
  rows=[{'id':'case','family':'family','split':'test','arm':arm,'rep':rep,'calls':[{'name':'get_upcoming','arguments':{}}],'grade':{'strict':arm!='current_rules','tool_exact':True},'seconds':1} for arm in a.ARMS for rep in range(3)]
  (p/'heldout.jsonl').write_text('\n'.join(map(json.dumps,rows)))
  return rows
 def test_complete_clustered_summary(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);self.fixture(p)
   with patch.object(a,'HERE',p),patch.object(sys,'argv',['analysis','--require-complete']):a.main()
   r=json.loads((p/'analysis.json').read_text())
   self.assertTrue(r['complete']);self.assertEqual(r['unique_test_cases'],1)
   self.assertEqual(r['arms']['ling_direct']['unique_cases_all_three_pass'],1)
   self.assertEqual(r['paired_family_differences']['ling_direct minus current_rules']['bootstrap_95pct_interval'],[1,1])
 def test_duplicate_rows_refused(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);rs=self.fixture(p)
   with (p/'heldout.jsonl').open('a') as f:f.write('\n'+json.dumps(rs[0]))
   with patch.object(a,'HERE',p),patch.object(sys,'argv',['analysis','--require-complete']),self.assertRaises(SystemExit):a.main()
 def test_corrupted_corpus_refused(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);self.fixture(p);(p/'corpus.json').write_text((p/'corpus.json').read_text()+' ')
   with patch.object(a,'HERE',p),patch.object(sys,'argv',['analysis','--require-complete']),self.assertRaises(SystemExit):a.main()
 def test_running_writer_partial_last_line(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'rows';p.write_text('{"id":1}\n{"id":')
   self.assertEqual(a.read_rows(p),[{'id':1}])
if __name__=='__main__':unittest.main()
