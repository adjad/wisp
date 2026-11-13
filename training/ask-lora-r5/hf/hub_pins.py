"""Compare Neo-Arid/Gemma-Wisp on the Hub with the manifest: top-level listing, LFS sha256 + size,
small non-LFS files downloaded and hashed. Exit 0 = pass, 1 = mismatch, 2 = not all files there yet."""
import hashlib, json, sys, urllib.request
REPO = "Neo-Arid/Gemma-Wisp"
MAN = sys.argv[1]
want = {}
for line in open(MAN):
    sha, size, name = line.split()
    want[name] = (sha, int(size))
import subprocess
def get(url):
    return subprocess.run(["curl", "-sfL", "-m", "120", url], check=True, capture_output=True).stdout
info = json.loads(get(f"https://huggingface.co/api/models/{REPO}"))
tree = json.loads(get(f"https://huggingface.co/api/models/{REPO}/tree/main?expand=true"))
print("commit", info.get("sha"), info.get("lastModified"))
names = {e["path"] for e in tree}
missing = sorted(set(want) - names)
extra = sorted(n for n in names - set(want) if not n.startswith("."))
if missing and not extra:
    print("NOT YET, missing:", ", ".join(missing)); sys.exit(2)
bad = []
for e in tree:
    n = e["path"]
    if n not in want:
        continue
    sha, size = want[n]
    lfs = e.get("lfs")
    if lfs:
        got = (lfs.get("oid"), int(lfs.get("size", -1)))
    else:
        data = get(f"https://huggingface.co/{REPO}/resolve/{info['sha']}/{n}")
        got = (hashlib.sha256(data).hexdigest(), len(data))
    ok = got == (sha, size)
    print(("OK  " if ok else "BAD ") + n, got[0][:12], got[1])
    if not ok:
        bad.append(f"{n}: hub {got[0][:12]} {got[1]} B, manifest {sha[:12]} {size} B")
for e in tree:
    if e.get("type") == "directory":
        print("DIR", e["path"])
if missing: print("MISSING:", ", ".join(missing))
if extra: print("EXTRA:", ", ".join(extra))
for b in bad: print("MISMATCH:", b)
sys.exit(0 if not (missing or extra or bad) else 1)
