#!/usr/bin/env python3
"""Assemble fresh author shards in isolated temp state, then emit deterministic data."""
import argparse,collections,hashlib,json,random,subprocess,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parent
SEED=610220261006
SHARDS=['arguments','context','grounded']
def dump_jsonl(path,rows):path.write_text(''.join(json.dumps(r,sort_keys=True,ensure_ascii=False,separators=(',',':'))+'\n' for r in rows))
def read(path):return [json.loads(x) for x in path.read_text().splitlines() if x]
def build(output):
 output.mkdir(parents=True,exist_ok=True)
 with tempfile.TemporaryDirectory(prefix='ling-v2-build-') as temp:
  dirs=[]
  for shard in SHARDS:
   dest=Path(temp)/shard;dest.mkdir();subprocess.run([sys.executable,str(ROOT/'shards'/shard/'generate.py'),'--output-dir',str(dest)],check=True);dirs.append(dest)
  for split in ['train','dev']:
   rows=[r for d in dirs for r in read(d/(split+'.jsonl'))];metas=[m for d in dirs for m in read(d/(split+'.provenance.jsonl'))];rows.sort(key=lambda r:r['id']);random.Random(SEED+(split=='dev')).shuffle(rows);byid={m['id']:m for m in metas};dump_jsonl(output/(split+'.jsonl'),rows);dump_jsonl(output/(split+'.provenance.jsonl'),[byid[r['id']] for r in rows])
  train=read(output/'train.jsonl');meta={m['id']:m for m in read(output/'train.provenance.jsonl')};groups=collections.defaultdict(list)
  for row in train:groups[meta[row['id']]['primary_group']].append(row)
  pilot=[]
  for name in sorted(groups):
   group=groups[name];random.Random(str(SEED)+name).shuffle(group);pilot.extend(group[:len(group)//2])
  random.Random(SEED+2).shuffle(pilot);dump_jsonl(output/'pilot-train.jsonl',pilot)
 manifest={'version':'2.0.0','seed':SEED,'contract_commit':'c313a459f6ab259e55981ffcb1bbe3df11fe9307','repository_base':'20e256d91bcb0bc34f5eb3a6c506bb9edc90f6fe','final_payloads_read_or_included':False,'model_inference_or_training_run':False,'files':{p.name:{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'rows':len(p.read_bytes().splitlines())} for p in sorted(output.glob('*.jsonl'))}}
 (output/'manifest.json').write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n');print(json.dumps({'output':str(output),'counts':{k:v['rows'] for k,v in manifest['files'].items()}},indent=2))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--output-dir',type=Path,default=ROOT);args=p.parse_args();build(args.output_dir)
