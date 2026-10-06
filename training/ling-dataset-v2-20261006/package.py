#!/usr/bin/env python3
"""Create allowlisted, inert train/development upload with reproducible ZIP headers."""
import hashlib,json,platform,zipfile,zlib
from pathlib import Path
ROOT=Path(__file__).resolve().parent
MEMBERS=['train.jsonl','dev.jsonl','pilot-train.jsonl','train.provenance.jsonl','dev.provenance.jsonl','intent.schema.v1.json','router-system.txt','DATA_CARD.md','README.md','preflight.py','qa.py','validate.py','manifest.json','validation.json']
def main():
 check=json.loads((ROOT/'validation.json').read_text());assert check['status']=='PASS_STATIC_DATA_QA'
 for section in ['validated_file_sha256','validator_sha256']:
  for name,expected in check[section].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==expected,'Stale validation: '+name
 for name in MEMBERS:assert (ROOT/name).is_file(),name
 member_meta={name:{'sha256':hashlib.sha256((ROOT/name).read_bytes()).hexdigest(),'bytes':(ROOT/name).stat().st_size} for name in MEMBERS}
 content={'artifact':'ling-train-dev-v2-20261006.zip','allowlisted_members':member_meta,'heldout_final_included':False,'training_ready_without_tokenizer_and_prepared_label_checks':False}
 (ROOT/'upload-manifest.json').write_text(json.dumps(content,sort_keys=True,indent=2)+'\n')
 path=ROOT/content['artifact']
 with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
  for name in sorted(MEMBERS+['upload-manifest.json']):
   info=zipfile.ZipInfo(name,date_time=(2026,10,6,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16;info.create_system=3;archive.writestr(info,(ROOT/name).read_bytes(),compresslevel=9)
 with zipfile.ZipFile(path) as archive:
  assert set(archive.namelist())==set(MEMBERS+['upload-manifest.json']);assert archive.testzip() is None
  for name,expected in member_meta.items():assert hashlib.sha256(archive.read(name)).hexdigest()==expected['sha256']
 receipt={'archive':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size,'members':len(MEMBERS)+1,'python':platform.python_version(),'zlib':zlib.ZLIB_VERSION,'static_data_only':True,'no_model_execution':True,'no_final_payload':True}
 (ROOT/'upload-zip.json').write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n');print(json.dumps(receipt,indent=2))
if __name__=='__main__':main()
