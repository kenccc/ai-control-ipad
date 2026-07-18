import json, subprocess, threading, time, os, glob
gs=json.load(open('/Users/ken/.codex/.codex-global-state.json'))
tpa=gs.get('thread-project-assignments') or {}
# pick most recent desktop-assigned thread ids
ids=list(tpa.keys())
ids.sort(reverse=True)  # uuidv7 sorts by time
cand=ids[:6]
print("desktop-assigned thread ids (newest):", cand)
# do rollout files exist for them?
for tid in cand[:6]:
    hits=glob.glob(f"/Users/ken/.codex/sessions/**/*{tid}*.jsonl", recursive=True)
    print(" ", tid, "rollout:", hits[0] if hits else "NOT FOUND")

proc = subprocess.Popen(["codex","app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, bufsize=1)
msgs=[]
def rd():
    for line in proc.stdout:
        try: msgs.append(json.loads(line))
        except: pass
threading.Thread(target=rd,daemon=True).start()
def send(i,m,p=None):
    d={"jsonrpc":"2.0","id":i,"method":m}
    if p is not None: d["params"]=p
    proc.stdin.write(json.dumps(d)+"\n"); proc.stdin.flush()
def wait(i,t=15):
    t0=time.time()
    while time.time()-t0<t:
        for m in msgs:
            if m.get("id")==i: return m
        time.sleep(0.2)
    return None
send(1,"initialize",{"clientInfo":{"name":"probe","title":"p","version":"0.1.0"}}); wait(1)
tid=cand[0]
send(2,"thread/read",{"threadId":tid})
r=wait(2)
print("\n=== thread/read", tid, "===")
print(json.dumps(r)[:2500] if r else "NO RESPONSE")
proc.terminate()
