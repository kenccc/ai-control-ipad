import json, subprocess, threading, sys, time

proc = subprocess.Popen(["codex","app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, bufsize=1)
out = []
def rd():
    for line in proc.stdout:
        out.append(line.rstrip())
threading.Thread(target=rd, daemon=True).start()

def send(i, method, params=None):
    msg = {"jsonrpc":"2.0","id":i,"method":method}
    if params is not None: msg["params"]=params
    proc.stdin.write(json.dumps(msg)+"\n"); proc.stdin.flush()

send(1,"initialize",{"clientInfo":{"name":"ai-control-probe","title":"AI Control","version":"0.1.0"}})
time.sleep(2.5)
send(2,"thread/list",{"pageSize":5})
time.sleep(2.5)
send(3,"thread/loaded/list",{})
time.sleep(2.0)
proc.terminate()
time.sleep(0.5)
for l in out[:40]:
    print(l[:1400])
err = proc.stderr.read()[:1500]
if err: print("STDERR:", err)
