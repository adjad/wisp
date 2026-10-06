import sys,os,json,stat,hashlib,time,datetime
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch
ROOT=Path('/Users/adijain/.codex/worktrees/router-update-integration/MOE_Project').resolve()
OUT=Path('/private/tmp/wisp-router-67e4cef-runtime-preflight-20261005').resolve()
HOME=Path('/Users/adijain')
INV=json.loads((OUT/'logs/invocation.json').read_text())
OVERLAY=Path(INV['observed_overlay_path'])
MODEL_SETTINGS=HOME/'.omlx/model_settings.json'
PACKAGED=ROOT/'service/config/models.yaml'
STATE=OUT/'logs/isolated-state'
STATE.mkdir(mode=0o700,exist_ok=True); (STATE/'.moe').mkdir(mode=0o700,exist_ok=True)
now=lambda:datetime.datetime.now(datetime.timezone.utc).isoformat()
report={'candidate':INV['candidate'],'observed_at_utc':now(),'configuration_sources':[],'roles':{},'errors':[],
 'selected_checkpoint':{'resolution':'not_attempted'},'runtime':{'constructors':0,'status_requests':0,'model_generations':0,'native_commands':0,'sockets':0,'loaded_weight_binding':'unverified','residency':'not inspected'},
 'isolation':{'stores':str(STATE),'environment':'fresh allowlist','guard_before_service_import':True,'credentials_settings_recovery_denied':True},'optional_memory_metadata':'not requested'}
checksums={'candidate':INV['candidate'],'disk_hashes_prove_loaded_weights':False,'files':[]}
blocked=[]; phase=None; selected_root=None; selected_files=set()
roots=(ROOT,Path(sys.prefix).resolve(),Path(sys.base_prefix).resolve(),OUT)
inside=lambda path,root:path==root or root in path.parents
admitted={OVERLAY,MODEL_SETTINGS,PACKAGED}
def audit(event,args):
 if event in {'socket.__new__','socket.connect','socket.getaddrinfo','subprocess.Popen','os.system','os.exec','os.posix_spawn','ctypes.dlopen'}:
  raise PermissionError('metadata preflight denies runtime/native operation')
 if event=='import' and args and (args[0] in {'service.main','service.inference.omlx_client'} or args[0].startswith(('service.tools.','mlx','torch','transformers'))):
  raise PermissionError('metadata preflight denies runtime module')
 if event=='open' and isinstance(args[0],(str,bytes,os.PathLike)):
  original=Path(os.fsdecode(args[0])).absolute(); actual=original.resolve(); mode,flags=args[1:3]
  writing=isinstance(mode,str) and any(c in mode for c in 'wax+') or isinstance(flags,int) and bool(flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC))
  if writing:
   if not inside(actual,OUT): raise PermissionError('metadata preflight denies installed writes')
  elif not any(inside(actual,root) for root in roots):
   if phase=='configuration' and original in admitted and original==actual: pass
   elif phase=='checkpoint' and original in selected_files and original==actual: pass
   else: raise PermissionError('metadata preflight denies non-admitted read')
  if original==HOME/'.omlx/settings.json' or original.parent==HOME/'.moe' and original.name.startswith('.'):
   raise PermissionError('metadata preflight denies credential/recovery file')
 if event in {'os.listdir','os.scandir'}:
  path=Path(os.fsdecode(args[0])).resolve() if args and isinstance(args[0],(str,bytes,os.PathLike)) else None
  if path is None or not any(inside(path,root) for root in roots) and path!=selected_root:
   raise PermissionError('metadata preflight denies directory traversal')
 if event in {'os.mkdir','os.remove','os.rmdir','os.chmod','sqlite3.connect'} and args and isinstance(args[0],(str,bytes,os.PathLike)):
  if not inside(Path(os.fsdecode(args[0])).resolve(),OUT): raise PermissionError('metadata preflight denies installed mutation')
 if event in {'os.rename','os.replace'} and any(not inside(Path(os.fsdecode(p)).resolve(),OUT) for p in args[:2]):
  raise PermissionError('metadata preflight denies installed mutation')
def profile(frame,event,arg):
 if event!='call':return
 file=frame.f_code.co_filename; name=frame.f_code.co_name
 if (file==str(ROOT/'service/config/__init__.py') and name in {'omlx_settings','omlx_api_key'}
  or file==str(ROOT/'service/config/endpoints.py') and name=='api_key'
  or file==str(ROOT/'service/config/credentials.py') and name in {'resolve','verify_binding','reviewed_bindings'}
  or file==str(ROOT/'service/config/quarantine.py') and name in {'generation','check','lease'}
  or file==str(ROOT/'service/config/provider_credentials.py') and name=='resolve_keychain'):
  blocked.append({'function':name,'source':str(Path(file).relative_to(ROOT))})
  raise PermissionError('credential/settings/recovery API excluded from metadata scope')
# Sanitized environment is created by the parent driver before this process starts.
assert not any(k.startswith('WISP_CREDENTIAL') or k.endswith('_KEY') or k.endswith('_TOKEN') for k in os.environ)
assert not any(k=='service' or k.startswith('service.') for k in sys.modules)
os.environ['WISP_HOME']=str(STATE/'.moe'); os.environ['WISPAIR_HOME']=str(STATE)
sys.dont_write_bytecode=True; Path.home=classmethod(lambda cls:STATE)
sys.addaudithook(audit); sys.setprofile(profile); sys.path.insert(0,str(ROOT))
# File reads are streamed; qualification/stability is checked before and after.
def signature(info):return (info.st_dev,info.st_ino,info.st_uid,info.st_mode,info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
def qualify(path):
 if path.is_symlink() or path.resolve()!=path:raise PermissionError('symlink metadata artifact rejected')
 info=path.lstat()
 if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o022:
  raise PermissionError('unqualified metadata artifact')
 return info
def opaque_hash(path,category):
 before=qualify(path); digest=hashlib.sha256()
 with path.open('rb') as stream:
  if signature(os.fstat(stream.fileno()))!=signature(before):raise PermissionError('artifact replaced before hash')
  while True:
   data=stream.read(1024*1024)
   if not data:break
   digest.update(data)
  if signature(os.fstat(stream.fileno()))!=signature(before):raise PermissionError('artifact changed during hash')
 after=path.lstat()
 if signature(after)!=signature(before):raise PermissionError('artifact changed after hash')
 row={'path':str(path),'category':category,'sha256':digest.hexdigest(),'bytes':before.st_size,'mtime_ns':before.st_mtime_ns,'device':before.st_dev,'inode':before.st_ino,'uid':before.st_uid,'mode':oct(stat.S_IMODE(before.st_mode)),'stable_before_after':True}
 checksums['files'].append(row);return row

def failure(stage,error):report['errors'].append({'stage':stage,'type':type(error).__name__})
try:
 from service import config as cfg
 from service.config.endpoints import role_target
 import yaml
 originals=(cfg.USER_CONFIG,cfg.OMLX_MODEL_SETTINGS,cfg.OMLX_SETTINGS)
 phase='configuration'
 try:
  with ExitStack() as stack:
   stack.enter_context(patch.object(cfg,'USER_CONFIG',OVERLAY)); stack.enter_context(patch.object(cfg,'OMLX_MODEL_SETTINGS',MODEL_SETTINGS))
   def deny_settings():
    blocked.append({'function':'omlx_settings','source':'service/config/__init__.py','guard':'explicit denied dependency'})
    raise PermissionError('credential-bearing settings API excluded')
   stack.enter_context(patch.object(cfg,'omlx_settings',deny_settings))
   for func in (cfg._user_overlay,cfg.models_config,cfg._packaged_config):func.cache_clear()
   for path,label in ((PACKAGED,'packaged'),(OVERLAY,'configured_overlay'),(MODEL_SETTINGS,'nonsecret_model_settings')):
    item={'path':str(path),'kind':label,'exists':path.exists()}
    if item['exists']:
     try:opaque_hash(path,label);item['qualified_stable']=True
     except Exception as error:failure(label,error);item['qualified_stable']=False;raise
    report['configuration_sources'].append(item)
   # Fail rather than accepting _user_overlay's corrupt-file fallback.
   if OVERLAY.exists():
    value=yaml.safe_load(OVERLAY.read_text())
    if value is not None and not isinstance(value,dict):raise ValueError('invalid overlay')
   settings=json.loads(MODEL_SETTINGS.read_text()) if MODEL_SETTINGS.exists() else {}
   if not isinstance(settings,dict) or not isinstance(settings.get('models',{}),dict):raise ValueError('invalid model metadata')
   effective=cfg.models_config()
   selected=set()
   for role in ('router','fast','agent','general','coding','reasoning'):
    entry={}
    try:
     entry['configured_model']=cfg.role_to_model(role);selected.add(entry['configured_model'])
     target=role_target(role)
     entry.update({'target_complete':True,'model':target.model,'revision':target.revision,'profile':target.profile,'context_window':target.context_window,'capabilities':list(target.capabilities),'dimensions':target.dimensions,
      'endpoint':{'name':target.endpoint.name,'origin':target.endpoint.base_url,'provider':target.endpoint.provider,'api_prefix':target.endpoint.api_prefix,'managed':target.endpoint.managed,'credential_reference':target.endpoint.credential_ref}})
    except Exception as error:
     entry.update({'target_complete':False,'error_type':type(error).__name__});failure('target_'+role,error);sys.setprofile(profile)
    report['roles'][role]=entry
   router=report['roles'].get('router',{})
   router_model=router.get('model') or router.get('configured_model')
   report['selected_model_ids']=sorted(selected)
   selected_settings=settings.get('models',{}).get(router_model,{})
   if not isinstance(selected_settings,dict):raise ValueError('invalid selected model metadata')
   safe_fields=('max_context_window','revision','model_path','model_dir','model_directory','path','hf_repo_id','repo_id')
   projection={key:selected_settings[key] for key in safe_fields if key in selected_settings and isinstance(selected_settings[key],(str,int,bool,float))}
   report['selected_model_settings']={'model':router_model,'metadata':projection,'source':str(MODEL_SETTINGS)}
   # Only a current selected-model reference can authorize checkpoint inventory.
   refs=[projection[key] for key in ('model_path','model_dir','model_directory','path') if isinstance(projection.get(key),str)]
   if len(set(refs))==1 and Path(refs[0]).is_absolute():
    proposed=Path(refs[0]); info=proposed.lstat()
    if proposed.is_symlink() or proposed.resolve()!=proposed or not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o022:
     raise PermissionError('selected checkpoint root unqualified')
    selected_root=proposed
    report['selected_checkpoint']={'model':router_model,'root':str(proposed),'reference_source':str(MODEL_SETTINGS),'reference_fields':[k for k in ('model_path','model_dir','model_directory','path') if k in projection],'resolution':'qualified_current_reference','loaded_weight_binding':'unverified'}
   else:
    report['selected_checkpoint']={'model':router_model,'resolution':'unavailable_no_unambiguous_absolute_selected_model_reference','broader_cache_or_home_scan_performed':False,'loaded_weight_binding':'unverified'}
 finally:
  phase=None
  for func in (cfg._user_overlay,cfg.models_config,cfg._packaged_config):func.cache_clear()
  report['configuration_constants_restored']=(cfg.USER_CONFIG,cfg.OMLX_MODEL_SETTINGS,cfg.OMLX_SETTINGS)==originals
 if selected_root:
  phase='checkpoint'; inventory=[]
  children=sorted(selected_root.iterdir(),key=lambda path:path.name)
  if len(children)>100:raise PermissionError('selected checkpoint inventory exceeds finite bound')
  for path in children:
   info=path.lstat()
   inventory.append({'name':path.name,'bytes':info.st_size,'mtime_ns':info.st_mtime_ns,'kind':'regular' if stat.S_ISREG(info.st_mode) else 'symlink' if stat.S_ISLNK(info.st_mode) else 'other'})
   if path.suffix in {'.safetensors','.gguf','.bin'} or path.name in {'config.json','model.safetensors.index.json','tokenizer_config.json','generation_config.json','README.md'}:
    qualify(path);selected_files.add(path)
  report['selected_checkpoint']['inventory']=inventory
  for path in sorted(selected_files):opaque_hash(path,'selected_checkpoint')
  report['selected_checkpoint']['opaque_hash_pass']='complete_named_artifacts'
except BaseException as error:failure('preflight',error)
finally:
 phase=None;sys.setprofile(None)
 report['denied_api_attempts']=blocked
 report['finished_at_utc']=now();report['scope_status']='metadata_only_no_runtime'
 (OUT/'metadata.json').write_text(json.dumps(report,indent=2)+'\n')
 (OUT/'checksums.json').write_text(json.dumps(checksums,indent=2)+'\n')
 print(json.dumps({'scope_status':report['scope_status'],'role_count':len(report['roles']),'incomplete_targets':[r for r,t in report['roles'].items() if not t.get('target_complete')],'checkpoint_resolution':report['selected_checkpoint']['resolution'],'errors':report['errors'],'runtime_calls':0}))
