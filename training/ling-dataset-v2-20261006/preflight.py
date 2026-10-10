#!/usr/bin/env python3
"""USER-RUN Brev tokenizer preflight; no weights, training, or tool execution."""
import argparse,hashlib,json
from pathlib import Path
MODEL='inclusionAI/Ling-3.0-tiny';REVISION='9a98e35fe1c9ee255f78dd64771c7ae15a799481'
TEMPLATE='eb6226c94ae38058f875d159f86a206b3a165828c0e7d6bda664ae14667f798a'
def main():
 p=argparse.ArgumentParser();p.add_argument('--data-dir',type=Path,default=Path(__file__).resolve().parent);p.add_argument('--local-files-only',action='store_true');args=p.parse_args()
 from transformers import AutoTokenizer
 tok=AutoTokenizer.from_pretrained(MODEL,revision=REVISION,trust_remote_code=True,local_files_only=args.local_files_only)
 assert hashlib.sha256(tok.chat_template.encode()).hexdigest()==TEMPLATE,'Pinned template drift'
 result={}
 for name in ['train','dev','pilot-train']:
  rows=[json.loads(x) for x in (args.data_dir/(name+'.jsonl')).read_text().splitlines() if x];lengths=[];targets=[]
  for row in rows:
   full=tok.apply_chat_template(row['messages'],tokenize=True,return_dict=False,add_generation_prompt=False,enable_thinking=False)
   prefix=tok.apply_chat_template(row['messages'][:-1],tokenize=True,return_dict=False,add_generation_prompt=True,enable_thinking=False)
   assert full[:len(prefix)]==prefix,'Prefix mismatch:'+row['id'];assert 0<len(full)-len(prefix) and len(full)<=2048,'Target/truncation:'+row['id'];lengths.append(len(full));targets.append(len(full)-len(prefix))
  result[name]={'rows':len(rows),'max_tokens':max(lengths),'median_tokens':sorted(lengths)[len(lengths)//2],'max_target_tokens':max(targets)}
 receipt={'status':'PASS_TOKENIZER_ONLY','model':MODEL,'revision':REVISION,'chat_template_sha256':TEMPLATE,'splits':result,'actual_axolotl_prepared_label_audit_still_required':True}
 (args.data_dir/'tokenizer-preflight.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt,indent=2))
if __name__=='__main__':main()
