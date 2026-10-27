import time, json, sys
from drive import turn
out=[]
for p in ["hello","explain what a bloom filter is in two sentences"]:
    time.sleep(8)  # let background pollers go quiet-ish
    a=time.time(); sid,ev,total,fd,text=turn(p); b=time.time()
    out.append((p,a,b,total))
json.dump(out,open('windows.json','w'))
print(out)
