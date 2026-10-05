"""Line-delimited local inference subprocess; no server or personal-data access."""
import os,sys,json,time,resource
os.environ['HF_HUB_OFFLINE']='1'
os.environ['LAYA_COREML_CACHE']='/private/tmp/wisp-routing-eval-coreml-cache'
from common import LAYA_QUESTION,NOW
import laya_coreml as laya
model='/Users/adijain/.cache/huggingface/hub/models--aac6fef--laya-multilingual-coreml/snapshots/8139e9089273319512c730218903784074133187'
t=time.perf_counter()
agent=laya.load(model,local_files_only=True,compute_units='cpu_gpu')
print(json.dumps({'ready':True,'load_seconds':time.perf_counter()-t,'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}),flush=True)
for line in sys.stdin:
 try:
  item=json.loads(line);c=item['case'];q=item.get('question',LAYA_QUESTION)
  state=[{'role':'system','content':f'Current time {NOW} America/Los_Angeles.'}]+c.get('context',[])+[{'role':'user','content':c['prompt']}]
  t=time.perf_counter();r=agent.predict(state,q)
  print(json.dumps({'id':c['id'],'result':r,'seconds':time.perf_counter()-t,'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}),flush=True)
 except Exception as e: print(json.dumps({'error':type(e).__name__+': '+str(e)}),flush=True)
