import json, subprocess, threading, time, collections
proc = subprocess.Popen(["codex","app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, bufsize=1)
res={}
def rd():
    for line in proc.stdout:
        try: m=json.loads(line)
        except: continue
        if "id" in m and "result" in m: res[m["id"]]=m["result"]
threading.Thread(target=rd, daemon=True).start()
def send(i,method,params=None):
    msg={"jsonrpc":"2.0","id":i,"method":method}
    if params is not None: msg["params"]=params
    proc.stdin.write(json.dumps(msg)+"\n"); proc.stdin.flush()
send(1,"initialize",{"clientInfo":{"name":"probe","title":"p","version":"0.1.0"}})
time.sleep(2)
cur=None; all_t=[]
for i in range(2,8):
    p={"pageSize":100}
    if cur: p["cursor"]=cur
    send(i,"thread/list",p)
    t0=time.time()
    while i not in res and time.time()-t0<8: time.sleep(0.2)
    r=res.get(i) or {}
    all_t += r.get("data",[])
    cur=r.get("nextCursor")
    if not cur: break
proc.terminate()
print("TOTAL THREADS:", len(all_t))
print("source counts:", collections.Counter(t.get("source") for t in all_t))
print("threadSource counts:", collections.Counter(str(t.get("threadSource")) for t in all_t))
print("status counts:", collections.Counter(str((t.get("status") or {}).get("type")) for t in all_t))
print("\nSAMPLE per source:")
seen=set()
for t in all_t:
    s=t.get("source")
    if s in seen: continue
    seen.add(s)
    print(" ", s, "|", t.get("name"), "| cwd:", t.get("cwd"), "| branch:", (t.get("gitInfo") or {}).get("branch"), "| ts:", t.get("threadSource"))
