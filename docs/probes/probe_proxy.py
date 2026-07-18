import json, subprocess, threading, time, sys
B="/Applications/ChatGPT.app/Contents/Resources/codex"
SOCK=sys.argv[1] if len(sys.argv)>1 else "/Users/ken/.codex/ipc/ipc.sock"
proc = subprocess.Popen([B,"app-server","proxy","--sock",SOCK], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
lines=[]
def rd():
    for l in proc.stdout: lines.append(l.rstrip())
threading.Thread(target=rd,daemon=True).start()
def send(i,m,p=None):
    d={"jsonrpc":"2.0","id":i,"method":m}
    if p is not None: d["params"]=p
    try:
        proc.stdin.write(json.dumps(d)+"\n"); proc.stdin.flush()
    except Exception as e: print("write fail",e)
send(1,"initialize",{"clientInfo":{"name":"ai-control-probe","title":"AI Control","version":"0.1.0"}})
time.sleep(2.5)
send(2,"thread/loaded/list",{})
time.sleep(2.5)
proc.terminate(); time.sleep(0.4)
print("--- STDOUT ---")
for l in lines[:15]: print(l[:900])
print("--- STDERR ---")
print(proc.stderr.read()[:900])
