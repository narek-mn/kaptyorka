#!/bin/bash
# Slice 3 of 07-design-v2.md (section 13): a box chat on the hub, end to end, in a fresh throw-away container.
# A real kaptyorka hub (a wheel built from this checkout, installed in its own venv) runs inside the container on
# loopback with TLS; alice's laptop is installed in personal mode from the ONE installer file built from this
# checkout (kap_installer.py; no repository in the container), with the two commands the hub's invite page prints
# (the hash check, then the install from the real personal invite link; each kind of bad link is refused first, with
# nothing changed); a STUB claude in the
# box fires Claude Code's hook JSON and owns a mailbox socket. A second fake machine (team machine box-1) sends
# a message to the box chat through the hub; it must arrive in the stub's socket, and the stub's reply (through
# the box's MCP server, the helper, the relay) must arrive back at box-1. Then the box chat asks for a helper chat
# (work), which must start inside the box with the Claude token on a descriptor only. Then the terminal filter
# (design 4.8): the stub prints clipboard, title, DCS, APC and report codes through the real launcher at a real
# terminal (script(1) in tmux); none may reach it, colours and a resize must. Last: rejoin with a new link and
# repair, both from the installer's own copy. Results: slice3-results.tsv.
#
# On the host (never as root):   ./slice3.sh            # fresh container kaptest-s3, removed at the end
#                                KEEP=1 ./slice3.sh     # leave it running
# REPO=<kaptyorka checkout> (default: the checkout this script is in). PY=<python with the build package> (default
# python3) builds the hub's wheel.
#
# Hard limits (07, section 13): every docker exec runs under `timeout -k 5 90`; every phase and helper runs in its
# own transient unit with RuntimeMaxSec and KillMode=control-group (polled with short execs); the product's box
# units get RuntimeMaxSec=150s from the installer. No host folder is mounted. Nothing runs as root on the host.
set -u
C=${C:-kaptest-s3}
ALL_PHASES="setup hubvenv hub install chat close term rejoin"

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
  # the laptop gets ONE file, the installer (as from the release page); the hub gets a wheel. No repository inside.
  PY=${PY:-python3}
  WB=$(mktemp -d)
  "$PY" -I "$REPO/src/kaptyorka/secure/kap_installer.py" build "$WB/kaptyorka-secure-setup.pyz" | cut -d' ' -f1 > "$WB/pyz.sha256" \
    || { rm -rf "$WB"; exit 1; }
  mkdir -p "$WB/repo" && cp "$REPO/pyproject.toml" "$REPO/README.md" "$REPO/LICENSE" "$WB/repo/" \
    && tar -C "$REPO" --exclude=__pycache__ --exclude='*.egg-info' -cf - src | tar -C "$WB/repo" -xf - \
    && timeout -k 5 300 "$PY" -m build --wheel --no-isolation --outdir "$WB/whl" "$WB/repo" >/dev/null 2>&1 \
    || { echo "cannot build the hub's wheel (PY=$PY needs the build package)"; rm -rf "$WB"; exit 1; }
  X mkdir -p /root/hubwheel /etc/kaptest /home/alice/Downloads
  timeout -k 5 90 docker cp "$WB/kaptyorka-secure-setup.pyz" "$C":/home/alice/Downloads/kaptyorka-secure-setup.pyz >/dev/null \
    && timeout -k 5 90 docker cp "$WB/pyz.sha256" "$C":/etc/kaptest/pyz.sha256 >/dev/null \
    && timeout -k 5 90 docker cp "$(ls "$WB"/whl/*.whl)" "$C":/root/hubwheel/ >/dev/null || { rm -rf "$WB"; exit 1; }
  rm -rf "$WB"
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
    case $p in hubvenv) lim=400;; chat) lim=170;; install|rejoin) lim=150;; *) lim=85;; esac
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
HUBPY="env KAPTYORKA_HOME=/var/lib/kaphub/home /opt/hubvenv/bin/python3"
PYZ=/home/alice/Downloads/kaptyorka-secure-setup.pyz
SETUP="python3 -I $PYZ"
L=/usr/local/libexec/kaptyorka

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
  mkdir -p /root/tools/bin /root/tools/testkit /var/lib/kaphub/home/tls /etc/kaptest
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
if os.path.exists(".stub-esc"):  # the terminal filter's test: codes that act through the terminal, colours, a resize
    def size(): c, r = os.get_terminal_size(1); return f"SIZE {c}x{r}"
    w = sys.stdout
    w.write("\x1b]52;c;Q0FOQVJZLUNMSVA=\x07\x1b]0;EVILTITLE\x07\x1bP+q544e\x1b\\\x1b_Gf=100;QUFBQQ==\x1b\\"
            "\x1b]1337;File=name=eA==:QUFB\x07\x1b[21t\x1b]8;;https://evil.example/\x1b\\LINKTEXT\x1b]8;;\x1b\\"
            "\x1b[31mRED-OK\x1b[0m VISIBLE-MARK " + size() + "\r\n"); w.flush()
    got = []
    signal.signal(signal.SIGWINCH, lambda *_: got.append(1))
    for _ in range(int(open(".stub-esc").read().strip() or "80")):
        time.sleep(0.1)
        if got: got.clear(); w.write("RESIZED " + size() + "\r\n"); w.flush()
    w.write("STUB-END\r\n"); w.flush(); sys.exit(0)
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
time.sleep(float(open(".stub-s").read()) if os.path.exists(".stub-s") else float(os.environ.get("STUB_S", "140")))
term()
EOF
  install -m 755 /root/stubclaude /root/tools/bin/claude
  # the hub's certificate (loopback), its seed (people, project, a team machine, a personal invite) and its app
  cd /etc/kaptest
  openssl req -x509 -newkey rsa:2048 -nodes -keyout hub.key -out hub.pem -days 2 -subj /CN=hub \
    -addext subjectAltName=IP:127.0.0.1 2>/dev/null
  openssl x509 -in hub.pem -outform der | sha256sum | cut -d' ' -f1 > hub.fp
  cp hub.pem /var/lib/kaphub/home/tls/hub.crt   # the hub's own TLS files: its invite links carry the fingerprint
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
# the links as the hub's tls.invite_url makes them, by name (the installer resolves it; the relay keeps the address)
fp = Path("/etc/kaptest/hub.fp").read_text().strip()
link = lambda code, pin=fp: f"https://localhost:8443/i/{code}#sha256={pin}"
Path("/etc/kaptest/link").write_text(link(Path("/etc/kaptest/invite").read_text()))
used = s.invite_create(alice, kind="personal", by="root"); s.invite_redeem(used, "old-laptop")
Path("/etc/kaptest/link-used").write_text(link(used))
Path("/etc/kaptest/link-expired").write_text(link(s.invite_create(alice, kind="personal", ttl_s=0, by="root")))
Path("/etc/kaptest/link-rejoin").write_text(link(s.invite_create(alice, kind="personal", by="root")))
team = s.invite_create(alice, by="root"); Path("/etc/kaptest/code-team").write_text(team)
Path("/etc/kaptest/link-team").write_text(link(team))
fpcode = s.invite_create(alice, kind="personal", by="root"); Path("/etc/kaptest/code-fp").write_text(fpcode)
Path("/etc/kaptest/link-fp").write_text(link(fpcode, "0" * 64))
for i, bad in enumerate(('https://localhost:8443/i/a"],"x', "https://localhost\nIPAddressAllow=any:8443/i/a",
                         "https://local;id:8443/i/a")):
    Path(f"/etc/kaptest/bad-{i}").write_text(f"{bad}#sha256={fp}")
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
  python3 -m venv /opt/hubvenv && /opt/hubvenv/bin/pip install -q /root/hubwheel/*.whl 'mcp==2.3.0' >/root/pip.log 2>&1
  t "S3-1 the hub's packages" "mcp 2.3.0 and uvicorn importable" 're:^rc=0 ok$' "$HUBPY -c 'import mcp, uvicorn, kaptyorka.hub, kaptyorka.hub_box; print(\"ok\")'"
  ;;

hub)
  $HUBPY /root/seed.py
  systemd-run --quiet --unit=kaptest-hub -p RuntimeMaxSec=1500 -p KillMode=control-group -p TimeoutStopSec=5 \
    -p Environment=KAPTYORKA_HOME=/var/lib/kaphub/home /opt/hubvenv/bin/python3 /root/hub.py
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
  t "S3-5 the team machine's session is on the hub" "box-1/claude-code/proj-a online" 're:box-1/claude-code/proj-a' \
    "$HUBPY /root/q.py 'SELECT name FROM agents WHERE last_seen > ?'"
  ;;

install)
  printf 'sk-ant-oat01-FAKE-SETUP-TOKEN-s3-not-a-login\n' > /root/claudetok; chmod 600 /root/claudetok
  OPTS="--person alice --name alice-laptop --claude-token-file /root/claudetok --runtime-max 150s --tools-from /root/tools --tools none"
  ONE="$SETUP install --personal"
  # unused <file with a code>: 1 while that invite is not used
  printf '#!/bin/bash\n%s /root/q.py "SELECT used_at IS NULL FROM invites WHERE code='"'"'$(cat /etc/kaptest/$1)'"'"'"\n' "$HUBPY" > /root/unused
  chmod 700 /root/unused
  # every bad link first: one sentence with a next step, rc 2, and nothing on the laptop (checked after them)
  t "I-2a a plain http:// link is refused (TLS is mandatory in personal mode)" "ask the admin to switch it on" \
    're:^rc=2 kaptyorka-secure-setup: this link starts with http://.*switch it on.*Nothing was changed' \
    "$ONE 'http://127.0.0.1:8080/i/\$(cat /etc/kaptest/invite)' $OPTS"
  t "I-2b a link without its fingerprint is refused (never taken from the network)" "no fingerprint" \
    're:^rc=2 .*no fingerprint at its end.*Nothing was changed' "$ONE \"\$(cut -d'#' -f1 /etc/kaptest/link)\" $OPTS"
  t "I-4 a wrong fingerprint: refused before any byte is sent, the invite not used" "not the one in the link; unused" \
    're:^rc=0 .*is not the one in the link \(its fingerprint differs\), so nothing was sent.*Nothing was changed.* exit=2 unused=1$' \
    "$ONE \"\$(cat /etc/kaptest/link-fp)\" $OPTS; echo \" exit=\$? unused=\$(/root/unused code-fp)\""
  t "I-2c a team-machine invite is refused by kind, the invite not used" "for a team machine; unused" \
    're:^rc=0 .*this link is for a team machine \(kaptyorka join\), not for a personal laptop: ask your team admin.* exit=2 unused=1$' \
    "$ONE \"\$(cat /etc/kaptest/link-team)\" $OPTS; echo \" exit=\$? unused=\$(/root/unused code-team)\""
  t "I-2d a used link is refused" "already used" 're:^rc=2 .*this link was already used \(a link works once\): ask your team admin' \
    "$ONE \"\$(cat /etc/kaptest/link-used)\" $OPTS"
  t "I-2e an expired link is refused" "not valid any more" 're:^rc=2 .*this link is not valid any more.*ask your team admin' \
    "$ONE \"\$(cat /etc/kaptest/link-expired)\" $OPTS"
  t "I-1 a link with unit or JSON text in it is refused" "refused, 3 times" 're:^rc=0 (refused ){3}$' \
    "for f in /etc/kaptest/bad-*; do $ONE \"\$(cat \$f)\" $OPTS >/dev/null 2>&1 && echo -n 'ACCEPTED ' || echo -n 'refused '; done"
  t "I-nochange after every refusal nothing of kaptyorka is on the laptop" "nothing" 're:^rc=0 $' \
    "ls -d /etc/kaptyorka /usr/local/libexec/kaptyorka /opt/kaptyorka /etc/sudoers.d/kaptyorka-* /etc/systemd/system/kap* /etc/profile.d/kaptyorka.sh 2>/dev/null; getent passwd kap-alice; true"
  # the hub's invite page: the two commands, the installer of the hub's own version, its hash from the hub's own files
  curl -sk "https://127.0.0.1:8443/i/$(cat /etc/kaptest/invite)" > /root/page.html
  python3 -I -c 'import html, re; m = re.search(r"<pre>(.*?)</pre>", open("/root/page.html").read(), re.S)
open("/root/page.cmds", "w").write(html.unescape(m.group(1)) + "\n" if m else "")'
  t "I-page the hub's invite page prints the download from the release of its own version, and its own installer's hash" \
    "curl -fLO .../releases/download/v<version>/kaptyorka-secure-setup.pyz; the hash of the file built on the host" \
    "re:^rc=0 curl -fLO https://github\\.com/narek-mn/kaptyorka/releases/download/v[0-9.]+/kaptyorka-secure-setup\\.pyz echo '$(cat /etc/kaptest/pyz.sha256)  kaptyorka-secure-setup\\.pyz' \\| sha256sum -c && sudo python3 -I kaptyorka-secure-setup\\.pyz install --personal 'https://127\\.0\\.0\\.1:8443/i/$(cat /etc/kaptest/invite)#sha256=$(cat /etc/kaptest/hub.fp)' \$" \
    "tr '\n' ' ' < /root/page.cmds"
  t "I-nogit no kaptyorka source on the laptop side: the installer file is all there is" "only the hub's venv has kap_setup.py" 're:^rc=0 $' \
    "find / -xdev \\( -path /proc -o -path /opt/hubvenv \\) -prune -o \\( -name kap_setup.py -o -name 'kaptyorka-*.whl' \\) -print 2>/dev/null | grep -v '^/root/hubwheel/'; ls -d /root/bundle /root/src 2>/dev/null; true"
  t "I-0 installer, personal mode: the page's second command as printed (hash check, then the install), in alice's Downloads" \
    "OK; joined; self-check passes" \
    're:^rc=0 kaptyorka-secure-setup\.pyz: OK.*Joining the hub at 127\.0\.0\.1:8443.*Joined: this computer is alice-laptop on the hub \(address 127\.0\.0\.1\).*Self-check: all walls in place.*Done\. This computer is alice-laptop.*A copy of this installer stays in /usr/local/libexec/kaptyorka/installer' \
    "cd /home/alice/Downloads && bash -c \"\$(sed -n 2p /root/page.cmds) $OPTS\""
  t "I-copy the installer keeps itself, root-owned: the same file" "755 root, same bytes; kaptyorka-secure-setup -> installer" \
    're:^rc=0 755 root /usr/local/libexec/kaptyorka/installer same installer$' \
    "stat -c '%a %U %n' $L/installer | tr '\n' ' '; cmp -s $L/installer $PYZ && echo -n 'same '; readlink $L/kaptyorka-secure-setup"
  sha256sum /etc/kaptyorka/secret/device-token | cut -d' ' -f1 > /root/devtok-1.sha
  t "I-conf the relay got the address it connected to, the link's port and pin, the key root-only for LoadCredential" \
    "hub_ip=127.0.0.1 hub_port=8443 pin; 600 root; IPAddressAllow=127.0.0.1; invite used (unused=0)" \
    "re:^rc=0 hub_ip=127\\.0\\.0\\.1 hub_port=8443 hub_sha256=$(cat /etc/kaptest/hub.fp) name=alice-laptop \\| 600 root root /etc/kaptyorka/secret/device-token \\| IPAddressAllow=127\\.0\\.0\\.1 \\| unused=0\$" \
    "grep -E '^(hub_|name=)' /etc/kaptyorka/relay.conf | tr '\n' ' '; echo -n '| '; stat -c '%a %U %G %n' /etc/kaptyorka/secret/device-token | tr '\n' ' '; echo -n '| '; grep IPAddressAllow /etc/systemd/system/kaptyorka-relay.service.d/hub.conf | tr '\n' ' '; echo \"| unused=\$(/root/unused invite)\""
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
    "for p in /api/me /api/feed '/bridge/poll?machine=x'; do curl -sk -o /dev/null -w '%{http_code} ' -H \"Authorization: Bearer \$(cat /etc/kaptyorka/secret/device-token)\" \"https://127.0.0.1:8443\$p\"; done | sed 's/ \$//'"
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

term)
  # design 4.8 at a real terminal: script(1) records every byte that reaches it; tmux gives script its terminal
  cat > /root/term.sh <<'EOF'
#!/bin/bash
exec script -qfec "runuser -u alice -- bash -c 'cd ~/team/proj-a && exec /usr/local/libexec/kaptyorka/launch claude'" /root/term.raw
EOF
  mkdir -p /root/ctl && echo 30 > /root/ctl/.stub-esc
  cat > /root/ctl.sh <<'EOF'
#!/bin/bash
cd /root/ctl && exec script -qfec "python3 -I /root/tools/testkit/stub-claude.py" /root/ctl.raw
EOF
  chmod 755 /root/term.sh /root/ctl.sh
  rm -f /root/term.raw /root/ctl.raw
  tmux -L s3 new-session -d -s ctl -x 120 -y 40 "/root/ctl.sh; sleep 20"
  wait_for 15 "grep -q STUB-END /root/ctl.raw"
  t "T-0 control: the stub really prints the codes (run as root, no box, no filter)" "OSC 52, title, DCS, APC, title report" \
    're:^rc=0 52 title dcs apc 21t $' \
    "f=/root/ctl.raw; grep -q \$'\\e]52;c;' \$f && echo -n '52 '; grep -q EVILTITLE \$f && echo -n 'title '; grep -q \$'\\eP+q' \$f && echo -n 'dcs '; grep -q \$'\\e_G' \$f && echo -n 'apc '; grep -q \$'\\e\\[21t' \$f && echo -n '21t '"
  echo 80 > /root/esc; install -o kap-alice -g kap-alice -m 644 /root/esc $PA/.stub-esc
  tmux -L s3 new-session -d -s term -x 120 -y 40 "/root/term.sh; sleep 30"
  wait_for 30 "grep -q VISIBLE-MARK /root/term.raw"
  t "T-1 the filter runs in its own unit as a throw-away user with no network (while the chat runs)" "DynamicUser yes, PrivateNetwork yes, not root" \
    're:^rc=0 yes yes [1-9][0-9]*$' \
    "u=\$(systemctl list-units --plain --no-legend 'kaptyorka-ttyfilter-*' | awk '{print \$1}' | head -1); echo \$(systemctl show -p DynamicUser -p PrivateNetwork --value \$u) \$(ps -o uid= -p \$(systemctl show -p MainPID --value \$u))"
  cat > /root/tiocsti.sh <<'EOF2'
#!/bin/bash
A=(); while read -r p; do A+=(-p "$p"); done < <(python3 -I -c 'import sys; sys.path.insert(0, "/usr/local/libexec/kaptyorka"); import kapcommon as kc; print("\n".join(kc.tty_filter_props("60s")))')
exec systemd-run --quiet --collect --pipe --wait "${A[@]}" -- /usr/bin/python3 -I -c 'import fcntl, termios
try:
    fcntl.ioctl(0, termios.TIOCSTI, b"x"); print("TIOCSTI-WORKED")
except OSError as e:
    print("TIOCSTI refused:", e.strerror)'
EOF2
  chmod 755 /root/tiocsti.sh
  # EIO: the kernel's dev.tty.legacy_tiocsti=0 refuses it for anyone without CAP_SYS_ADMIN; EPERM: with it on, the
  # terminal is not the unit's own. Either way, refused (the value is printed)
  t "T-1b the filter's unit cannot type into the terminal it holds (TIOCSTI)" "refused" \
    're:TIOCSTI refused: (Operation not permitted|Input/output error) legacy_tiocsti=[01]' \
    ": > /root/tiocsti.out; tmux -L s3 new-session -d -s sti '/root/tiocsti.sh > /root/tiocsti.out 2>&1; sleep 5'; timeout 20 bash -c 'until [ -s /root/tiocsti.out ]; do sleep 0.5; done'; echo \$(cat /root/tiocsti.out) legacy_tiocsti=\$(cat /proc/sys/dev/tty/legacy_tiocsti 2>/dev/null || echo none)"
  tmux -L s3 resize-window -t term -x 90 -y 30; sleep 2
  wait_for 15 "grep -q STUB-END /root/term.raw"
  t "T-2 colours, text and the first screen size reach the terminal through the box and the filter" "RED-OK in red, SIZE 120x40" \
    're:^rc=0 red size$' "f=/root/term.raw; grep -q \$'\\e\\[31mRED-OK' \$f && echo -n 'red '; grep -q 'VISIBLE-MARK SIZE 120x40' \$f && echo size"
  t "T-3 a resize reaches the box (SIGWINCH and the new size)" "RESIZED SIZE 90x30" 're:RESIZED SIZE 90x30' "grep -a RESIZED /root/term.raw"
  t "T-4 no clipboard (OSC 52), title, file transfer, DCS, APC, title report or disguised link reaches the terminal" "none of them; the link's text stays" \
    're:^rc=0 LINKTEXT$' \
    "f=/root/term.raw; grep -q \$'\\e]52' \$f && echo -n 'OSC52 '; grep -q EVILTITLE \$f && echo -n 'TITLE '; grep -q \$'\\e]1337' \$f && echo -n 'OSC1337 '; grep -q \$'\\eP' \$f && echo -n 'DCS '; grep -q \$'\\e_' \$f && echo -n 'APC '; grep -q \$'\\e\\[21t' \$f && echo -n '21t '; grep -q 'evil.example' \$f && echo -n 'LINK '; grep -ao LINKTEXT \$f"
  wait_for 15 "! systemctl list-units --plain --no-legend 'kaptyorka-ttyfilter-*' | grep -q ."
  t "T-5 the filter's unit ends with the chat" "none left" 're:^rc=0 $' "systemctl list-units --plain --no-legend 'kaptyorka-ttyfilter-*'"
  rm -f $PA/.stub-esc; tmux -L s3 kill-server 2>/dev/null; true
  ;;

rejoin)
  # a new personal link (as after a new certificate or address): from the installer's own copy, nothing downloaded
  t "R-1 rejoin with a new link, by name, from the copy in libexec" "joined again" \
    're:^rc=0 .*Joining the hub at localhost:8443.*Joined again: this computer is alice-laptop on the hub \(address 127\.0\.0\.1\)' \
    "$L/installer rejoin \"\$(cat /etc/kaptest/link-rejoin)\""
  t "R-2 the device key changed, the hub has the new device, the rest of relay.conf kept" "new key; 3 personal devices of alice (old-laptop, this one, the new one)" \
    're:^rc=0 changed 3 name=alice-laptop$' \
    "[ \"\$(sha256sum /etc/kaptyorka/secret/device-token | cut -d' ' -f1)\" != \"\$(cat /root/devtok-1.sha)\" ] && echo -n 'changed '; $HUBPY /root/q.py \"SELECT count(*) FROM devices d JOIN people p ON p.id=d.person_id WHERE p.name='alice' AND d.mode='personal'\" | tr '\n' ' '; grep '^name=' /etc/kaptyorka/relay.conf"
  cp /etc/kaptyorka/relay.conf /root/relay.conf.1
  t "R-3 the same link again is refused and changes nothing" "already used; relay.conf the same" \
    're:^rc=0 .*already used.* exit=2 same$' \
    "$L/installer rejoin \"\$(cat /etc/kaptest/link-rejoin)\"; echo \" exit=\$?\" \$(cmp -s /etc/kaptyorka/relay.conf /root/relay.conf.1 && echo same)"
  t "R-4 repair from the copy writes everything again and the self-check passes" "Repaired" \
    're:^rc=0 .*Self-check: all walls in place.*Repaired: every file and unit is as this installer writes them' "$L/installer repair"
  # a chat after rejoin reaches the hub with the new key
  echo 25 > /root/stubs; install -o kap-alice -g kap-alice -m 644 /root/stubs $PA/.stub-s
  systemd-run --quiet --unit=kaptest-chat2 -p RuntimeMaxSec=60 -p KillMode=control-group -p User=root \
    -p StandardOutput=file:/root/chat2.log -p StandardError=inherit \
    /usr/sbin/runuser -u alice -- bash -c 'cd ~/team/proj-a && exec /usr/local/libexec/kaptyorka/launch claude'
  wait_for 40 "$HUBPY /root/q.py 'SELECT name FROM agents WHERE box=1 AND last_seen > ?' | grep -q ."
  t "R-5 after rejoin a box chat is on the hub again (the relay uses the new key)" "alice-laptop/box/proj-a" \
    're:^rc=0 alice-laptop/box/proj-a' "$HUBPY /root/q.py 'SELECT name FROM agents WHERE box=1 AND last_seen > ?'"
  systemctl stop kaptest-chat2.service 2>/dev/null; rm -f $PA/.stub-s; true
  ;;
esac
exit 0
