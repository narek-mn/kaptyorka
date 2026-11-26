#!/bin/bash
# Slice 3 of 07-design-v2.md (section 13): a box chat on the hub, end to end, in a fresh throw-away container.
# A real kaptyorka hub (the hub app of this checkout) runs inside the container on loopback with TLS;
# alice's laptop is installed in personal mode with a device token from a personal invite; a STUB claude in the
# box fires Claude Code's hook JSON and owns a mailbox socket. A second fake machine (team machine box-1) sends
# a message to the box chat through the hub; it must arrive in the stub's socket, and the stub's reply (through
# the box's MCP server, the helper, the relay) must arrive back at box-1. Then the box chat asks for a helper chat
# (work), which must start inside the box with the Claude token on a descriptor only. Results: slice3-results.tsv.
#
# On the host (never as root):   ./slice3.sh            # fresh container kaptest-s3, removed at the end
#                                KEEP=1 ./slice3.sh     # leave it running
# REPO=<kaptyorka checkout> (default: the checkout this script is in).
#
# Hard limits (07, section 13): every docker exec runs under `timeout -k 5 90`; every phase and helper runs in its
# own transient unit with RuntimeMaxSec and KillMode=control-group (polled with short execs); the product's box
# units get RuntimeMaxSec=150s from the installer. No host folder is mounted. Nothing runs as root on the host.
set -u
C=${C:-kaptest-s3}
ALL_PHASES="setup hubvenv hub install chat close"

if [ "${1:-}" != "--inner" ]; then
  HERE=$(cd "$(dirname "$0")" && pwd)
  REPO=${REPO:-$(cd "$HERE/../.." && pwd)}
  X() { timeout -k 5 90 docker exec "$C" "$@"; }
  unit() { # name limit: run `slice3.sh --inner <name>` as a unit, poll until done (each poll bounded)
    local n=$1 lim=$2 i s
    X systemd-run --quiet --unit="s3-$n" -p RuntimeMaxSec="$lim" -p KillMode=control-group -p TimeoutStopSec=5 \
      -p Environment=HOME=/root -p StandardOutput=file:/root/s3-$n.log -p StandardError=inherit \
      /bin/bash /root/slice3.sh --inner "$n" || { echo "phase $n did not start"; return 1; }
    for i in $(seq $((lim / 3 + 3))); do
      s=$(X systemctl show -p ActiveState --value "s3-$n.service" 2>/dev/null)
      case $s in active|activating|deactivating) sleep 3;; *) break;; esac
    done
    s=$(X systemctl show -p Result --value "s3-$n.service" 2>/dev/null)
    if [ "$s" != success ]; then
      X bash -c "printf 'PHASE %s\tFAIL\tphase ends with success\t(phase unit)\tresult=%s (TIMEOUT if timeout)\n' '$n' '$s' >> /root/results.tsv"
    fi
  }
  [ -f "$REPO/src/kaptyorka/hub_box.py" ] || { echo "missing $REPO/src/kaptyorka/hub_box.py"; exit 1; }
  docker image inspect kaptest-box >/dev/null 2>&1 || timeout -k 5 900 docker build -t kaptest-box "$HERE" || exit 1
  timeout -k 5 90 docker rm -f "$C" >/dev/null 2>&1
  timeout -k 5 90 docker run -d --name "$C" --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
    --tmpfs /run --tmpfs /run/lock kaptest-box >/dev/null || exit 1
  for i in $(seq 60); do s=$(X systemctl is-system-running 2>/dev/null); case $s in running|degraded) break;; esac; sleep 1; done
  X mkdir -p /root/bundle /root/src
  # the secure parts as the installer finds them, and the whole package for the hub
  tar -C "$REPO/src" --exclude=__pycache__ -cf - kaptyorka/__init__.py kaptyorka/secure kaptyorka/relay \
    | timeout -k 5 90 docker exec -i "$C" tar -C /root/bundle -xf - || exit 1
  tar -C "$REPO/src" --exclude=__pycache__ -cf - kaptyorka | timeout -k 5 90 docker exec -i "$C" tar -C /root/src -xf - || exit 1
  timeout -k 5 90 docker cp "$0" "$C":/root/slice3.sh >/dev/null || exit 1
  # The stub claude is a tiny static program (execute-only 0711 after install, as the real one): box-exec and the
  # helper hand the Claude token only to a claude program the box cannot read. It runs the stub script.
  SB=$(mktemp -d); printf '%s\n' '#include <stdlib.h>' '#include <unistd.h>' \
    'int main(int c, char **v) { char **a = calloc(c + 3, sizeof *a); a[0] = "python3"; a[1] = "-I";' \
    '  a[2] = "/opt/kap-tools/testkit/stub-claude.py"; for (int i = 1; i < c; i++) a[i + 2] = v[i];' \
    '  execv("/usr/bin/python3", a); return 127; }' > "$SB/stub.c"
  gcc -static -O2 -o "$SB/stubclaude" "$SB/stub.c" && timeout -k 5 90 docker cp "$SB/stubclaude" "$C":/root/stubclaude >/dev/null \
    || { rm -rf "$SB"; exit 1; }
  rm -rf "$SB"
  X bash -c ': > /root/results.tsv'
  for p in ${PHASES:-$ALL_PHASES}; do
    case $p in hubvenv) lim=400;; chat) lim=170;; *) lim=85;; esac
    echo "== phase $p (limit ${lim}s)"; unit "$p" "$lim"
  done
  X bash -c 'systemctl kill --signal=KILL "kapbox-*.slice" kaptest-chat.service >/dev/null 2>&1; true'
  timeout -k 5 90 docker cp "$C":/root/results.tsv "$HERE/slice3-results.tsv" >/dev/null
  X bash -c 'cut -f2 /root/results.tsv | sort | uniq -c'
  X bash -c 'grep -P "\tFAIL\t" /root/results.tsv | cut -f1,5 | cut -c1-400'
  echo "results: $HERE/slice3-results.tsv"
  if [ -z "${KEEP:-}" ]; then
    timeout -k 5 90 docker rm -f "$C" >/dev/null && echo "container $C removed"
  fi
  exit 0
fi

########################################################################################
# Inside the container
########################################################################################
export DEBIAN_FRONTEND=noninteractive HOME=/root
umask 022
PH=$2
RES=/root/results.tsv
KH=/var/lib/kap-team/kap-alice
PA=$KH/projects/proj-a
HUBPY="env PYTHONPATH=/root/src KAPTYORKA_HOME=/var/lib/kaphub/home /opt/hubvenv/bin/python3"
SETUP="python3 -I /root/bundle/kaptyorka/secure/kap_setup.py"

rec() { # id status expected command actual
  local a; a=$(printf '%s' "$5" | tr '\n\t\r' '   ' | sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g; s/  */ /g' | cut -c1-600)
  local c; c=$(printf '%s' "$4" | tr '\n\t\r' '   ')
  printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$c" "$a" >> "$RES"; printf '[%s] %s: %s\n' "$2" "$1" "$a"
}
t() { # ID "expected" CHECK 'command'     CHECK: re:REGEX | nore:REGEX | info
  local id=$1 exp=$2 chk=$3 cmd=$4 out rc st
  out=$(timeout -k 2 "${TL:-25}" bash -c "$cmd" 2>&1 </dev/null); rc=$?
  [ $rc -eq 124 ] && out="TIMEOUT $out"
  out="rc=$rc $out"
  case $chk in
    re:*)   if [[ $out =~ ${chk#re:} ]]; then st=PASS; else st=FAIL; fi;;
    nore:*) if [[ $out =~ ${chk#nore:} ]]; then st=FAIL; else st=PASS; fi;;
    info)   st=INFO;;
  esac
  [[ $out == rc=124* ]] && st=FAIL
  rec "$id" "$st" "$exp" "$cmd" "$out"
}
# wait_for SECONDS 'shell test': poll every second, at most SECONDS
wait_for() { local i; for i in $(seq "$1"); do bash -c "$2" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
export -f wait_for

case $PH in
setup)
  mount --make-rshared /   # docker's --tmpfs /run is private: systemd's own mounts need a shared /
  apt-get update -qq >/dev/null 2>&1; apt-get install -y -qq --no-install-recommends openssh-client acl >/root/apt.log 2>&1
  rm -rf /home/alice/team
  runuser -u alice -- bash -c 'umask 022; mkdir -p ~/team/proj-a ~/personal ~/.local/bin && echo "team readme" > ~/team/proj-a/README.md
    echo CANARY-PERSONAL-FILE-7f3a > ~/personal/diary.txt; echo CANARY-CLAUDE-MD-6d6d > ~/.claude/CLAUDE.md'
  chown -R alice:alice /home/alice; chmod 700 /home/alice
  mkdir -p /root/tools/bin /root/tools/testkit /var/lib/kaphub/home /etc/kaptest
  # the STUB claude for the box: Claude Code's hooks from --settings with the documented hook JSON and
  # environment (CLAUDE_CODE_MESSAGING_SOCKET, _TOKEN, CLAUDE_CODE_SESSION_ID), its own mailbox socket, and a
  # reply through the MCP server of --mcp-config. Everything it sees goes to .stub/<session>.log in its folder.
  cat > /root/tools/testkit/stub-claude.py <<'EOF'
import ctypes, json, os, re, secrets, signal, socket, subprocess, sys, threading, time, uuid
ctypes.CDLL(None).prctl(15, b"claude", 0, 0, 0)  # PR_SET_NAME: the process is named claude, as the real one
a = sys.argv[1:]; settings = mcpconf = None
while a and a[0] in ("--settings", "--mcp-config", "--strict-mcp-config"):
    if a[0] == "--strict-mcp-config": a = a[1:]; continue
    if a[0] == "--settings": settings = a[1]
    else: mcpconf = a[1]
    a = a[2:]
if a[:1] == ["agents"]: print("[]"); sys.exit(0)
def take_token():  # as Claude Code: the descriptor up to a newline, closed, the variable dropped
    fd = os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "")
    if fd.isdigit():
        buf = b""
        try:
            while not buf.endswith(b"\n"):
                g = os.read(int(fd), 4096)
                if not g: break
                buf += g
            os.close(int(fd))
        except OSError: pass
        return "fd", buf.decode("ascii", "replace").strip()
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"): return "env", os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    return "none", ""
how, tokval = take_token()
tokline = (how + (" ok" if tokval.startswith("sk-ant-oat01-FAKE-SETUP-TOKEN-s3") else " bad")
           + " ENV " + ("leak" if tokval and any(tokval in v for v in os.environ.values()) else "clean")
           + " SCRUB " + ("set" if "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB" in os.environ else "unset"))
if a[:1] == ["-p"]:  # a helper chat: where it runs and how it got its token
    cg = open("/proc/self/cgroup").read().strip().rsplit(":", 1)[-1]
    os.makedirs(".stub-helper", exist_ok=True)
    with open(f".stub-helper/{os.getpid()}.log", "a") as f:
        f.write(f"HELPER USER {os.getuid()} TOKEN {tokline} CG {cg} PROMPT {' '.join(a[1:])[:200]}\n")
    print("helper chat:", a[1:]); sys.exit(0)
sid = str(uuid.uuid4()); tok = secrets.token_hex(8)
os.makedirs(".stub", exist_ok=True)
logf = open(f".stub/{sid}.log", "a", buffering=1)
def log(*x): logf.write(" ".join(str(y) for y in x).replace("\n", "\\n") + "\n")
log("SID", sid, "PID", os.getpid(), "USER", os.getuid(), "TOKEN", tokline)
path = f"/tmp/cc-stub-{sid[:8]}.sock"
srv = socket.socket(socket.AF_UNIX); srv.bind(path); os.chmod(path, 0o600); srv.listen(8)
env = {**os.environ, "CLAUDE_CODE_MESSAGING_SOCKET": path, "CLAUDE_CODE_MESSAGING_TOKEN": tok, "CLAUDE_CODE_SESSION_ID": sid}
hooks = json.load(open(settings)).get("hooks", {}) if settings else {}
def fire(event, **extra):
    data = {"session_id": sid, "cwd": os.getcwd(), "hook_event_name": event, "transcript_path": f"/tmp/{sid}.jsonl", **extra}
    for g in hooks.get(event, []):
        for h in g.get("hooks", []):
            r = subprocess.run(["/bin/sh", "-c", h["command"]], input=json.dumps(data), capture_output=True, text=True, env=env, timeout=20)
            log("HOOK", event, "rc", r.returncode, "OUT", r.stdout.strip()[:3000], "ERR", r.stderr.strip()[:300])
def mcp_call(tool, args, label):
    m = json.load(open(mcpconf))["mcpServers"]["kaptyorka"]
    p = subprocess.Popen([m["command"], *m["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}}]
    out, _ = p.communicate("".join(json.dumps(x) + "\n" for x in msgs), timeout=40)
    log(label, out.strip().splitlines()[-1][:600] if out.strip() else "nothing")
def mcp_send(to, text): mcp_call("send", {"to": to, "text": text}, "SENT")
def serve():
    while True:
        c, _ = srv.accept()
        buf = b""
        while True:
            g = c.recv(65536)
            if not g: break
            buf += g
        c.close()
        for line in buf.decode().splitlines():
            m = json.loads(line)
            if m.get("type") == "auth": log("AUTH", "ok" if m.get("token") == tok else "WRONG")
            elif m.get("type") == "user":
                text = m["message"]["content"]; log("INBOX", text)
                to = re.search(r'send\(to="([^"]+)"\)', text)
                n = re.search(r"PING-([0-9a-f]+)", text)
                if to and n: threading.Thread(target=mcp_send, args=(to.group(1), f"REPLY-FROM-BOX-CHAT-{n.group(1)}"), daemon=True).start()
                w = re.search(r"WORK-([0-9a-f]+)", text)
                if w: threading.Thread(target=mcp_call, args=("work", {"prompt": f"HELPER-TASK-{w.group(1)}"}, "WORKED"), daemon=True).start()
threading.Thread(target=serve, daemon=True).start()
def term(*_):
    log("TERM"); fire("SessionEnd", reason="other"); os.unlink(path); sys.exit(0)
signal.signal(signal.SIGTERM, term)
fire("SessionStart", source="startup")
fire("UserPromptSubmit", prompt="hello CANARY-PROMPT-TEXT-0b0b")
fire("Stop", stop_hook_active=False)
time.sleep(float(os.environ.get("STUB_S", "140")))
term()
EOF
  install -m 755 /root/stubclaude /root/tools/bin/claude
  # the hub's certificate (loopback), its seed (people, project, a team machine, a personal invite) and its app
  cd /etc/kaptest
  openssl req -x509 -newkey rsa:2048 -nodes -keyout hub.key -out hub.pem -days 2 -subj /CN=hub \
    -addext subjectAltName=IP:127.0.0.1 2>/dev/null
  openssl x509 -in hub.pem -outform der | sha256sum | cut -d' ' -f1 > hub.fp
  cat > /root/seed.py <<'EOF'
from pathlib import Path
from kaptyorka.store import Store
from kaptyorka.manager import ManagerStore
s = Store(Path("/var/lib/kaphub/hub.db")); ManagerStore(s).set_enabled(False, "test")
root = s.person_create("root", "admin"); alice = s.person_create("alice")
s.project_register("proj-a", by="root"); s.grant_project("proj-a", alice, "write", "root")
tm, code = s.team_machine_add("box-1", "root")
tok, _ = s.invite_redeem(code, "box-1", "box-1", "team")
Path("/etc/kaptest/tm.token").write_text(tok)
Path("/etc/kaptest/root.token").write_text(s.device_add(root, "root-desk"))
Path("/etc/kaptest/invite").write_text(s.invite_create(alice, kind="personal", by="root"))
EOF
  cat > /root/hub.py <<'EOF'
from pathlib import Path
import uvicorn
from kaptyorka.store import Store
from kaptyorka.manager import ManagerStore
from kaptyorka.hub import build_app
s = Store(Path("/var/lib/kaphub/hub.db")); ManagerStore(s).set_enabled(False, "test")
uvicorn.run(build_app(s), host="127.0.0.1", port=8443, ssl_certfile="/etc/kaptest/hub.pem",
            ssl_keyfile="/etc/kaptest/hub.key", log_level="warning")
EOF
  # the second fake machine's session (team machine box-1): MCP over https to the hub, as a shim would
  cat > /root/tm.py <<'EOF'
import asyncio, json, sys
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
H = {"Authorization": "Bearer " + open("/etc/kaptest/tm.token").read().strip(), "X-Kaptyorka-Key": "claude:tm-1",
     "X-Kaptyorka-Machine": "box-1", "X-Kaptyorka-Tool": "claude-code", "X-Kaptyorka-Project": "proj-a"}
async def go(tool, args):
    async with httpx2.AsyncClient(headers=H, verify=False) as hc:
        async with Client(streamable_http_client("https://127.0.0.1:8443/mcp", http_client=hc)) as c:
            r = await c.call_tool(tool, args)
            print(json.dumps(r.structured_content if r.structured_content else [x.text for x in r.content]))
tool = sys.argv[1]; args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
asyncio.run(go(tool, args))
EOF
  cat > /root/q.py <<'EOF'
import sqlite3, sys, time
d = sqlite3.connect("file:/var/lib/kaphub/hub.db?mode=ro", uri=True)
for r in d.execute(sys.argv[1], [time.time() - 30] if "?" in sys.argv[1] else []): print("|".join(map(str, r)))
EOF
  t "S3-0 setup" "stub, certificate, seed scripts" 're:^rc=0 [0-9a-f]{64}$' "test -x /root/tools/bin/claude && cat /etc/kaptest/hub.fp"
  ;;

hubvenv)
  python3 -m venv /opt/hubvenv && /opt/hubvenv/bin/pip install -q 'mcp==2.3.0' >/root/pip.log 2>&1
  t "S3-1 the hub's packages" "mcp 2.3.0 and uvicorn importable" 're:^rc=0 ok$' "$HUBPY -c 'import mcp, uvicorn, kaptyorka.hub, kaptyorka.hub_box; print(\"ok\")'"
  ;;

hub)
  $HUBPY /root/seed.py
  systemd-run --quiet --unit=kaptest-hub -p RuntimeMaxSec=1500 -p KillMode=control-group -p TimeoutStopSec=5 \
    -p Environment=PYTHONPATH=/root/src -p Environment=KAPTYORKA_HOME=/var/lib/kaphub/home /opt/hubvenv/bin/python3 /root/hub.py
  wait_for 30 'curl -sk -o /dev/null https://127.0.0.1:8443/'
  # the team machine's bridge: its session claude:tm-1 in proj-a, a heartbeat every 10 s
  cat > /root/beat.sh <<'EOF'
while true; do
  curl -sk -o /dev/null -X POST https://127.0.0.1:8443/bridge/heartbeat -H "Authorization: Bearer $(cat /etc/kaptest/tm.token)" \
    -H 'Content-Type: application/json' -d '{"machine":"box-1","bridge":true,"tools":[],"facts":{"host":"box-1","os_user":"team"},"sessions":[{"key":"claude:tm-1","tool":"claude-code","project":"proj-a"}]}'
  sleep 10; done
EOF
  systemd-run --quiet --unit=kaptest-tm-beat -p RuntimeMaxSec=1500 -p KillMode=control-group /bin/bash /root/beat.sh
  sleep 2
  t "S3-2 a real hub with TLS on loopback" "https answers" 're:^rc=0 [0-9]{3}$' "curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1:8443/"
  t "S3-3 kaptyorka join's request cannot use a personal invite (nothing used)" "refused: secure installer" 're:secure installer' \
    "curl -sk -X POST https://127.0.0.1:8443/i/\$(cat /etc/kaptest/invite) -H 'Content-Type: application/json' -d '{\"device_name\":\"x\",\"host\":\"h\",\"os_user\":\"alice\"}'"
  curl -sk -X POST "https://127.0.0.1:8443/i/$(cat /etc/kaptest/invite)" -H 'Content-Type: application/json' \
    -d '{"device_name":"alice-laptop","mode":"personal"}' > /root/redeem.json
  python3 -I -c 'import json; print(json.load(open("/root/redeem.json"))["token"])' > /root/devtok; chmod 600 /root/devtok
  t "S3-4 the personal invite through the installer's request" "a token, mode personal" 're:^rc=0 personal [A-Za-z0-9_-]{30,}$' \
    "python3 -I -c 'import json; d=json.load(open(\"/root/redeem.json\")); print(d[\"mode\"], d[\"token\"])'"
  t "S3-5 the team machine's session is on the hub" "box-1/claude-code/proj-a online" 're:box-1/claude-code/proj-a' \
    "$HUBPY /root/q.py 'SELECT name FROM agents WHERE last_seen > ?'"
  ;;

install)
  printf 'sk-ant-oat01-FAKE-SETUP-TOKEN-s3-not-a-login\n' > /root/claudetok; chmod 600 /root/claudetok
  t "I-0 installer, personal mode, real device token and pinned fingerprint" "self-check passes" 're:^rc=0 .*Self-check: all walls in place' \
    "$SETUP install --personal --person alice --hub-ip 127.0.0.1 --hub-port 8443 --hub-sha256 \$(cat /etc/kaptest/hub.fp) --name alice-laptop --device-token-file /root/devtok --claude-token-file /root/claudetok --runtime-max 150s --tools-from /root/tools"
  t "I-helper the box code holds the helper next to the proxy bridge, its fence module and the tools' wire (slice 5)" "fence.py, helper.py, proxybridge.py, toolwire.py" 're:^rc=0 fence.py helper.py proxybridge.py toolwire.py$' \
    "ls /opt/kaptyorka/box/current/ | tr '\n' ' ' | sed 's/ \$//'"
  t "I-profile the box's Claude Code settings: deny rules and the four command hooks, no tool or permission hooks" "as section 5.2" \
    're:^rc=0 deny-ok hooks=SessionEnd,SessionStart,Stop,UserPromptSubmit mem=/srv/team/alice/_memory$' \
    "python3 -I -c 'import json; s=json.load(open(\"/etc/kaptyorka/boxes/alice/claude-settings.json\")); d=s[\"permissions\"][\"deny\"]; print(\"deny-ok\" if {\"SendMessage\",\"ListAgents\"} <= set(d) else d, \"hooks=\"+\",\".join(sorted(s[\"hooks\"])), \"mem=\"+s[\"autoMemoryDirectory\"])'"
  t "H-add hubchat add proj-a as alice" "a team folder" 're:proj-a is now a team folder' "runuser -u alice -- bash -c 'hubchat add ~/team/proj-a' 2>&1"
  ;;

chat)
  # alice types `claude` in her team folder (no terminal: enter uses --pipe --wait, D8); the stub runs 140 s
  systemd-run --quiet --unit=kaptest-chat -p RuntimeMaxSec=160 -p KillMode=control-group -p User=root \
    -p StandardOutput=file:/root/chat.log -p StandardError=inherit \
    /usr/sbin/runuser -u alice -- bash -c 'cd ~/team/proj-a && exec /usr/local/libexec/kaptyorka/launch claude'
  wait_for 40 "ls $PA/.stub/*.log && grep -q 'HOOK Stop' $PA/.stub/*.log"
  LOG=$(ls $PA/.stub/*.log 2>/dev/null | head -1); SID=$(awk '/^SID/{print $2; exit}' "$LOG" 2>/dev/null)
  echo "$SID" > /root/sid
  t "C-1 the stub chat runs in the box as kap-alice, the token on a descriptor, in no environment" "USER=uid of kap-alice, TOKEN fd ok ENV clean" \
    "re:^rc=0 SID [0-9a-f-]{36} PID [0-9]+ USER $(id -u kap-alice) TOKEN fd ok ENV clean SCRUB unset" "head -1 $LOG"
  t "C-2 SessionStart: the standing rule and the hub's brief reach the chat" "rule + brief" 're:You are a team chat.*(Rules|proj-a|State)' \
    "grep '^HOOK SessionStart' $LOG"
  t "C-3 the helper unit runs inside the box" "active" 're:^rc=0 active$' "systemctl is-active kapbox-alice-helper.service"
  wait_for 25 "$HUBPY /root/q.py 'SELECT name FROM agents WHERE box=1 AND last_seen > ?' | grep -q ."
  t "C-4 the box chat is on the hub like other chats (tool box, its project, no session id, no folder)" "alice-laptop/box/proj-a" \
    're:^rc=0 alice-laptop/box/proj-a\|box\|proj-a\|\|box:[0-9]+:[0-9a-f]{16}$' \
    "$HUBPY /root/q.py 'SELECT name, tool, project, cwd, key FROM agents WHERE box=1 AND last_seen > ?'"
  t "C-5 who on the team machine lists the box chat (its project hidden: box-1 has no role in proj-a)" "alice-laptop/box/(private), tool box" \
    're:"name": "alice-laptop/box/\(private\)", "machine": "alice-laptop", "tool": "box"' "$HUBPY /root/tm.py who"
  N=$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n'); echo "$N" > /root/nonce
  t "C-6 the team machine sends to the box chat through the hub" "sent; the helper delivers" 're:sent to 1 session; a box chat on a personal laptop gets it from its box helper' \
    "$HUBPY /root/tm.py send '{\"to\": \"alice-laptop/box/proj-a\", \"text\": \"PING-$N from box-1: please answer\"}'"
  wait_for 30 "grep -q 'INBOX.*PING-$N' $LOG"
  t "C-7 the message arrives in the stub chat's mailbox socket, with its origin line" "INBOX ... PING" \
    "re:INBOX \\[kaptyorka: from box-1 on box-1, a team chat\\..*PING-$N from box-1" "grep 'INBOX' $LOG"
  t "C-7b the helper opened with the chat's own token (documented auth line)" "AUTH ok" 're:AUTH ok' "grep '^AUTH' $LOG"
  wait_for 25 "$HUBPY /root/q.py \"SELECT push_state FROM messages WHERE text LIKE '%PING-$N%'\" | grep -q pushed"
  t "C-8 the hub marks it delivered (the helper's ack)" "pushed" 're:^rc=0 pushed$' "$HUBPY /root/q.py \"SELECT push_state FROM messages WHERE text LIKE '%PING-$N%'\""
  wait_for 30 "grep -q '^SENT' $LOG"
  t "C-9 the stub chat replies through the box's MCP server" "sent to 1 session" 're:SENT .*sent to 1 session' "grep '^SENT' $LOG"
  t "C-10 the reply arrives back at the team machine's session" "REPLY-FROM-BOX-CHAT" "re:REPLY-FROM-BOX-CHAT-$N" "$HUBPY /root/tm.py inbox"
  t "C-7c the message body is in a data fence under the helper's origin line (red team round 2, finding 3)" "<<<data ... PING ... <<<end of data" \
    "re:INBOX \\[kaptyorka: from box-1 on box-1, a team chat\\.[^<]*<<<data [0-9a-f]{16}[^>]*>>>\\\\nPING-$N from box-1[^<]*<<<end of data [0-9a-f]{16}>>>" "grep 'INBOX' $LOG"
  # a helper chat (work) from the box chat: it must start inside the box, as box-exec starts a chat (round 2, observation)
  W=$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n')
  t "C-15 the team machine asks the box chat for a helper chat" "sent" 're:sent to 1 session' \
    "$HUBPY /root/tm.py send '{\"to\": \"alice-laptop/box/proj-a\", \"text\": \"WORK-$W please start a helper chat\"}'"
  wait_for 30 "grep -q '^WORKED' $LOG"
  t "C-16 the box chat asks for it with work" "a helper chat starts" 're:WORKED .*a helper chat starts in proj-a' "grep '^WORKED' $LOG"
  wait_for 30 "grep -qs HELPER-TASK-$W $PA/.stub-helper/*.log"
  t "C-17 the helper chat really runs inside the box: kap-alice, the helper's own unit, the token on a descriptor only, no scrub flag" \
    "HELPER USER kap-alice TOKEN fd ok ENV clean SCRUB unset CG ...kapbox-alice-helper.service" \
    "re:^rc=0 HELPER USER $(id -u kap-alice) TOKEN fd ok ENV clean SCRUB unset CG /[^ ]*kapbox-alice-helper\\.service PROMPT HELPER-TASK-$W" \
    "cat $PA/.stub-helper/*.log"
  t "C-11 the hub never holds the chat's session id, its folder path, the prompt or alice's personal data" "none of them" \
    're:^rc=0 $' "strings /var/lib/kaphub/hub.db /var/lib/kaphub/hub.db-wal 2>/dev/null | grep -o -e \"$SID\" -e 'CANARY-[A-Z-]*' -e '/srv/team/alice' -e 'kap-alice' | sort -u | tr '\n' ' '"
  t "C-12 the personal device reaches nothing but its box calls" "403 403 403" 're:^rc=0 403 403 403$' \
    "for p in /api/me /api/feed '/bridge/poll?machine=x'; do curl -sk -o /dev/null -w '%{http_code} ' -H \"Authorization: Bearer \$(cat /root/devtok)\" \"https://127.0.0.1:8443\$p\"; done | sed 's/ \$//'"
  t "C-13 the relay sent only box calls and refused nothing of the box" "relay: alice report ok" 're:relay: alice report ok' \
    "journalctl -u kaptyorka-relay.service --no-pager -q | tail -40"
  t "C-14 the relay holds no file of the person and runs under its own sandbox" "its view check passed" 're:^rc=0 active$' \
    "systemctl is-active kaptyorka-relay.service"
  ;;

close)
  LOG=$(ls $PA/.stub/*.log 2>/dev/null | head -1)
  KEY=$($HUBPY /root/q.py 'SELECT key FROM agents WHERE box=1 AND last_seen > ?' | head -1)
  HID=$($HUBPY -c "from kaptyorka.store import session_id; print(session_id('$KEY'))")
  t "X-1 the page closes the box chat (an admin's order, on the helper's next report)" "202 closing" 're:closing' \
    "curl -sk -X POST https://127.0.0.1:8443/api/sessions/\$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' '$HID')/close -H \"Authorization: Bearer \$(cat /etc/kaptest/root.token)\" -H 'Content-Type: application/json' -d '{\"confirm\": true}'"
  wait_for 25 "grep -q '^HOOK SessionEnd' $LOG"
  t "X-2 the helper ended the chat inside the box (SIGTERM to its own process); SessionEnd fired" "TERM, SessionEnd" 're:TERM.*HOOK SessionEnd' \
    "grep -e '^TERM' -e '^HOOK SessionEnd' $LOG | tr '\n' ' '"
  wait_for 20 "! $HUBPY /root/q.py 'SELECT key FROM agents WHERE box=1 AND last_seen > ?' | grep -q ."
  t "X-3 the box chat left the hub" "no box chat online" 're:^rc=0 $' "$HUBPY /root/q.py 'SELECT name FROM agents WHERE box=1 AND last_seen > ?'"
  t "X-4 nothing of the box ran outside its slice; alice has no kaptyorka process" "none" 're:^rc=0 0$' \
    "{ ps -u alice -o args= | grep -c -e helper.py -e relay.py -e kaptyorka; } || true"
  ;;
esac
exit 0
