import json,hashlib,ast,marshal,zipfile
from pathlib import Path
OLD=Path('/private/tmp/ling-dataset-v2-review-input-v2');NEW=Path('/private/tmp/ling-dataset-v2-review-input-v3');OUT=Path('/private/tmp/ling-dataset-v2-review/v3')
EXPECTED2='4fe27d9ec60788a234f857437af244c3c6ed2c935a5080049a126a7e39c2e391'
EXPECTED3='402e67d52f0547551d4f1b2f246ca80c296cc98dc592f71c1f6c58efa8b76383'
ZIP_SHA='3c2a45238f8cc4669b440e24fa9a18c49990d284eaa005ed238da5db65cb3ded'
def sha(data):return hashlib.sha256(data).hexdigest()
def hashes(root,want):
 raw=(root/'snapshot-manifest.json').read_bytes();assert sha(raw)==want;m=json.loads(raw)
 result={name:sha((root/name).read_bytes()) for name in m['files']}
 assert all(result[name]==v['sha256'] and (root/name).stat().st_size==v['bytes'] for name,v in m['files'].items())
 return dict(root=str(root),manifest_sha256=want,files=result),m
before2,m2=hashes(OLD,EXPECTED2);before3,m3=hashes(NEW,EXPECTED3)
assert len(m2['files'])==40 and len(m3['files'])==43
(OUT/'hashes-before.json').write_text(json.dumps({'snapshot2':before2,'snapshot3':before3},sort_keys=True,indent=2)+'\n')
path='shards/arguments/generate.py';changed=[n for n in m2['files'] if before2['files'][n]!=before3['files'][n]];assert changed==[path],changed
assert set(m3['files'])-set(m2['files'])=={'ling-train-dev-v2-20261006.zip','upload-manifest.json','upload-zip.json'}
old=(OLD/path).read_bytes();new=(NEW/path).read_bytes();a=old.splitlines(keepends=True);b=new.splitlines(keepends=True);assert len(a)==len(b)
lines=[]
for i,(x,y) in enumerate(zip(a,b),1):
 if x!=y:assert i in {156,160} and x.endswith(b' \n') and y==x[:-2]+b'\n';lines.append(i)
assert lines==[156,160] and len(old)-len(new)==2
old_ast=ast.dump(ast.parse(old),include_attributes=True);new_ast=ast.dump(ast.parse(new),include_attributes=True);assert old_ast==new_ast
# Compile, but never execute, source; constant filename binds identical metadata.
code_a=marshal.dumps(compile(old,'arguments-generator-binding','exec'));code_b=marshal.dumps(compile(new,'arguments-generator-binding','exec'));assert code_a==code_b
assert sha(new)=='98891f67995a6a95bfd0633455c6d5ea159f3ea99baa3ac79260d2c9620caa8d'
zip_path=NEW/'ling-train-dev-v2-20261006.zip';zreceipt=json.loads((NEW/'upload-zip.json').read_text());upload=json.loads((NEW/'upload-manifest.json').read_text())
assert sha(zip_path.read_bytes())==ZIP_SHA==zreceipt['sha256']==m3['files'][zip_path.name]['sha256']
assert zreceipt['bytes']==zip_path.stat().st_size==1316226
with zipfile.ZipFile(zip_path) as z:
 assert set(z.namelist())==set(upload['allowlisted_members'])|{'upload-manifest.json'} and len(z.namelist())==zreceipt['members']==15
 assert z.testzip() is None and z.read('upload-manifest.json')==(NEW/'upload-manifest.json').read_bytes()
 for name,meta in upload['allowlisted_members'].items():assert sha(z.read(name))==meta['sha256']==before3['files'][name] and len(z.read(name))==meta['bytes']
assert upload['training_ready_without_tokenizer_and_prepared_label_checks'] is False
result=dict(verdict='PASS_WITH_NOTES',scope='Mechanical binding update only; previous snapshot2 semantic/QA/runtime verdict retained for unchanged inputs',source_base=m3['base'],integration_base=m3['integration_base'],snapshot3_manifest_sha256=EXPECTED3,snapshot2_manifest_sha256=EXPECTED2,changed_existing_files=changed,removed_trailing_space_lines=lines,removed_bytes=2,source_ast_with_locations_equal=True,compiled_marshaled_code_equal=True,compiled_code_sha256=sha(code_b),unchanged_prior_files=39,arguments_generator_sha256=sha(new),zip_sha256=ZIP_SHA,zip_bytes=1316226,zip_members=15,zip_receipt_and_member_hashes_verified=True,semantic_qa_runtime_gates_repeated=False,pending_tokenizer_trainer_and_model_quality_limits_retained=True,formal_release_auditor_credit=False,initial_snapshot1_BLOCK_preserved=True,prior_snapshot2_PASS_WITH_NOTES_preserved=True,required_repairs=[],no_snapshot_candidate_writes=True)
(OUT/'mechanical-check.json').write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
after2,_=hashes(OLD,EXPECTED2);after3,_=hashes(NEW,EXPECTED3);assert before2==after2 and before3==after3
(OUT/'hashes-after.json').write_text(json.dumps({'snapshot2':after2,'snapshot3':after3,'unchanged':True},sort_keys=True,indent=2)+'\n');print(json.dumps(result,sort_keys=True))
