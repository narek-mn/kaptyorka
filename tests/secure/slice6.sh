#!/bin/bash
# Slice 6 (build plan 16): signed installs and signed self-updates of a personal laptop, end to end, in a fresh
# throw-away container. A real hub (a wheel of this checkout, its own venv, TLS on loopback) serves the signed release
# from its static folder (<hub home>/releases/current/), filled here the way the release tool fills it. Three
# installers (versions 50.0.1, 50.0.2 and 50.0.3, the last with a wallcheck broken on purpose) are built on the host
# from copies of this checkout whose release key was set with `kap_release.py set-key` to a THROW-AWAY key made here
# (no passphrase, never a real key), and signed with `kap_release.py prepare` (CI's file = the same build).
# Phase boot: the README's install lines, as written there (the link and the key put in), refuse a hub that serves a
# changed installer and a key that is not the release key, without using the link; then they install 50.0.1.
# Phase update: the laptop's updater (kap_update.py, the root timer's service) refuses an unsigned release, one signed
# by another key, a changed manifest, a changed installer, a hub serving another installer than the signed one, the
# same version and (later) an older one, with nothing run and nothing changed; it keeps a good 50.0.2 waiting while a
# team chat runs (hubchat list and a chat's start say so), installs it through the real unit once none runs; and
# rolls a 50.0.3 whose self-check fails back to 50.0.2, never trying 50.0.3 again. Results: slice6-results.tsv.
#
# On the host (never as root):   ./slice6.sh            # fresh container kaptest-s6, removed at the end
#                                KEEP=1 ./slice6.sh     # leave it running
# REPO=<kaptyorka checkout> (default: the checkout this script is in). PY=<python with the build package> (default
# python3) builds the hub's wheel.
#
# Hard limits: every docker exec runs under `timeout -k 5 90`; every phase runs in its own transient unit with
# RuntimeMaxSec and KillMode=control-group (polled with short execs); every check under `timeout`; every helper unit
# has RuntimeMaxSec. No host folder is mounted. Nothing runs as root on the host.
set -u
C=${C:-kaptest-s6}
ALL_PHASES="setup hubvenv hub boot update"
V1=50.0.1 V2=50.0.2 V3=50.0.3

if [ "${1:-}" != "--inner" ]; then
  HERE=$(cd "$(dirname "$0")" && pwd)
  REPO=${REPO:-$(cd "$HERE/../.." && pwd)}
  X() { timeout -k 5 90 docker exec "$C" "$@"; }
  unit() { # name limit: run `slice6.sh --inner <name>` as a unit, poll until done (each poll bounded)
    local n=$1 lim=$2 i s
    X systemd-run --quiet --unit="s6-$n" -p RuntimeMaxSec="$lim" -p KillMode=control-group -p TimeoutStopSec=5 \
      -p Environment=HOME=/root -p StandardOutput=file:/root/s6-$n.log -p StandardError=inherit \
      /bin/bash /root/slice6.sh --inner "$n" || { echo "phase $n did not start"; return 1; }
    for i in $(seq $((lim / 3 + 3))); do
      s=$(X systemctl show -p ActiveState --value "s6-$n.service" 2>/dev/null)
      case $s in active|activating|deactivating) sleep 3;; *) break;; esac
    done
    s=$(X systemctl show -p Result --value "s6-$n.service" 2>/dev/null)
    if [ "$s" != success ]; then
      X bash -c "printf 'PHASE %s\tFAIL\tphase ends with success\t(phase unit)\tresult=%s (TIMEOUT if timeout)\n' '$n' '$s' >> /root/results.tsv"
    fi
  }
  [ -f "$REPO/src/kaptyorka/secure/kap_release.py" ] || { echo "missing $REPO/src/kaptyorka/secure/kap_release.py"; exit 1; }
  command -v ssh-keygen >/dev/null || { echo "needs ssh-keygen on the host"; exit 1; }
  PY=${PY:-python3}
  WB=$(mktemp -d)
  trap 'rm -rf "$WB"' EXIT
  # the throw-away release key and a second key (another signer); never a real key, never a passphrase
  timeout 30 ssh-keygen -q -t ed25519 -N '' -C slice6-test-only -f "$WB/testkey" || exit 1
  timeout 30 ssh-keygen -q -t ed25519 -N '' -C slice6-other -f "$WB/otherkey" || exit 1
  # three checkouts "at a tag": this one's files, the version changed, the test key set by the release tool
  for v in $V1 $V2 $V3; do
    mkdir -p "$WB/t$v" && tar -C "$REPO" --exclude=__pycache__ --exclude='*.egg-info' -cf - src | tar -C "$WB/t$v" -xf - || exit 1
    sed -i "s/^__version__ = \".*\"/__version__ = \"$v\"/" "$WB/t$v/src/kaptyorka/__init__.py"
    timeout 30 "$PY" -I "$WB/t$v/src/kaptyorka/secure/kap_release.py" set-key "$WB/testkey.pub" >/dev/null || exit 1
  done
  "$PY" -I - "$WB/t$V3/src/kaptyorka/secure/kap_wallcheck.py" <<'EOF' || exit 1
import sys
p = sys.argv[1]; s = open(p).read()
s = s.replace('if __name__ == "__main__":\n', 'if __name__ == "__main__":\n    sys.exit("wallcheck: broken on purpose (slice 6)")\n', 1)
open(p, "w").write(s)
EOF
  for v in $V1 $V2 $V3; do
    T="$WB/t$v/src/kaptyorka/secure"
    timeout 60 "$PY" -I "$T/kap_installer.py" build "$WB/ci-$v.pyz" >/dev/null || exit 1
    timeout 60 "$PY" -I "$T/kap_release.py" prepare "$v" --ci "$WB/ci-$v.pyz" --key "$WB/testkey" --out "$WB/rel/v$v" \
      </dev/null >/dev/null || { echo "prepare $v failed"; exit 1; }
  done
  # the bad releases a hub could serve
  R=$WB/rel
  mkdir -p "$R/unsigned" "$R/otherkey" "$R/changed" "$R/changedpyz" "$R/wrongfile"
  cp "$R/v$V2/kaptyorka-secure-setup.pyz" "$R/v$V2/release.manifest" "$R/unsigned/"
  cp "$R/v$V2/kaptyorka-secure-setup.pyz" "$R/v$V2/release.manifest" "$R/otherkey/"
  timeout 30 ssh-keygen -q -Y sign -f "$WB/otherkey" -n kaptyorka-release "$R/otherkey/release.manifest" </dev/null || exit 1
  cp "$R/v$V2/"* "$R/changed/" && sed -i "s/^version=$V2\$/version=50.0.9/" "$R/changed/release.manifest"
  cp "$R/v$V2/"* "$R/changedpyz/" && printf '\0' >> "$R/changedpyz/kaptyorka-secure-setup.pyz"
  cp "$R/v$V2/release.manifest" "$R/v$V2/release.manifest.sig" "$R/wrongfile/" && cp "$R/v$V1/kaptyorka-secure-setup.pyz" "$R/wrongfile/"
  mkdir -p "$R/bootchanged" && cp "$R/v$V1/"* "$R/bootchanged/" && printf '\0' >> "$R/bootchanged/kaptyorka-secure-setup.pyz"
  cut -d' ' -f1,2 "$WB/testkey.pub" > "$WB/testkey.line"; cut -d' ' -f1,2 "$WB/otherkey.pub" > "$WB/otherkey.line"
  # the README's install lines, exactly as written there (phase boot runs them)
  "$PY" -I -c 'import re, sys; t = open(sys.argv[1]).read(); t = t[t.index("**3. Get the installer, check its signature"):]
m = re.search(r"```bash\n(.*?)```", t, re.S); sys.stdout.write(m.group(1) if m else "")' "$REPO/README.md" > "$WB/readme.cmds" \
    && [ -s "$WB/readme.cmds" ] || { echo "no install lines in the README"; exit 1; }
  # the hub: a wheel of this checkout
  mkdir -p "$WB/repo" && cp "$REPO/pyproject.toml" "$REPO/README.md" "$REPO/LICENSE" "$WB/repo/" \
    && tar -C "$REPO" --exclude=__pycache__ --exclude='*.egg-info' -cf - src | tar -C "$WB/repo" -xf - \
    && timeout -k 5 300 "$PY" -m build --wheel --no-isolation --outdir "$WB/whl" "$WB/repo" >/dev/null 2>&1 \
    || { echo "cannot build the hub's wheel (PY=$PY needs the build package)"; exit 1; }
  docker image inspect kaptest-box >/dev/null 2>&1 || timeout -k 5 900 docker build -t kaptest-box "$HERE" || exit 1
  timeout -k 5 90 docker rm -f "$C" >/dev/null 2>&1
  timeout -k 5 90 docker run -d --name "$C" --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
    --tmpfs /run --tmpfs /run/lock kaptest-box >/dev/null || exit 1
  for i in $(seq 60); do s=$(X systemctl is-system-running 2>/dev/null); case $s in running|degraded) break;; esac; sleep 1; done
  X mkdir -p /root/hubwheel /etc/kaptest /home/alice/Downloads || exit 1
  timeout -k 5 90 docker cp "$R" "$C":/root/rel >/dev/null \
    && timeout -k 5 90 docker cp "$WB/readme.cmds" "$C":/etc/kaptest/readme.cmds >/dev/null \
    && timeout -k 5 90 docker cp "$WB/testkey.line" "$C":/etc/kaptest/testkey.line >/dev/null \
    && timeout -k 5 90 docker cp "$WB/otherkey.line" "$C":/etc/kaptest/otherkey.line >/dev/null \
    && timeout -k 5 90 docker cp "$(ls "$WB"/whl/*.whl)" "$C":/root/hubwheel/ >/dev/null \
    && timeout -k 5 90 docker cp "$0" "$C":/root/slice6.sh >/dev/null || exit 1
  X bash -c ': > /root/results.tsv'
  for p in ${PHASES:-$ALL_PHASES}; do
    case $p in setup|hubvenv) lim=400;; boot) lim=300;; update) lim=900;; *) lim=85;; esac
    echo "== phase $p (limit ${lim}s)"; unit "$p" "$lim"
  done
  X bash -c 'systemctl kill --signal=KILL "kapbox-*.slice" >/dev/null 2>&1; true'
  timeout -k 5 90 docker cp "$C":/root/results.tsv "$HERE/slice6-results.tsv" >/dev/null
  X bash -c 'cut -f2 /root/results.tsv | sort | uniq -c'
  X bash -c 'grep -P "\tFAIL\t" /root/results.tsv | cut -f1,5 | cut -c1-400'
  echo "results: $HERE/slice6-results.tsv"
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
HUBPY="env KAPTYORKA_HOME=/var/lib/kaphub/home /opt/hubvenv/bin/python3"
RELS=/var/lib/kaphub/home/releases
L=/usr/local/libexec/kaptyorka
UPD="/usr/bin/python3 -I $L/kap_update.py"

rec() { # id status expected command actual
  local a; a=$(printf '%s' "$5" | tr '\n\t\r' '   ' | sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g; s/  */ /g' | cut -c1-700)
  local c; c=$(printf '%s' "$4" | tr '\n\t\r' '   ')
  printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$c" "$a" >> "$RES"; printf '[%s] %s: %s\n' "$2" "$1" "$a"
}
t() { # ID "expected" CHECK 'command'     CHECK: re:REGEX | nore:REGEX | info
  local id=$1 exp=$2 chk=$3 cmd=$4 out rc st
  out=$(timeout -k 2 "${TL:-40}" bash -c "$cmd" 2>&1 </dev/null); rc=$?
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
wait_for() { local i; for i in $(seq "$1"); do bash -c "$2" >/dev/null 2>&1 && return 0; sleep 1; done; return 1; }
# serve DIR: the hub's static folder offers this release (as kap_release.py publish switches it: one rename)
serve() { mkdir -p "$RELS" && rm -rf "$RELS/x" && cp -r "/root/rel/$1" "$RELS/x" && ln -sfn x "$RELS/current.new" \
  && mv -T "$RELS/current.new" "$RELS/current"; }
# what changed on the laptop: the version, the installer's copy, the waiting files, the update's result
lap() { printf 'version=%s installer=%s staged=%s result=%s' "$(cat /etc/kaptyorka/version)" \
  "$(sha256sum $L/installer | cut -c1-12)" "$(ls $L/update 2>/dev/null | grep -c staged)" \
  "$(python3 -I -c 'import json; print(json.load(open("/etc/kaptyorka/update.json")).get("result", ""))' 2>/dev/null)"; }
sha() { sha256sum "/root/rel/$1/kaptyorka-secure-setup.pyz" | cut -c1-12; }
export -f lap sha serve wait_for
export RELS L UPD

case $PH in
setup)
  mount --make-rshared /   # docker's --tmpfs /run is private: systemd's own mounts need a shared /
  timeout 200 apt-get update -qq >/dev/null 2>&1; timeout 160 apt-get install -y -qq --no-install-recommends openssh-client acl >/root/apt.log 2>&1
  rm -rf /home/alice/team
  runuser -u alice -- bash -c 'umask 022; mkdir -p ~/team/proj-a ~/personal && echo "team readme" > ~/team/proj-a/README.md
    echo CANARY-PERSONAL-FILE-7f3a > ~/personal/diary.txt'
  chown -R alice:alice /home/alice; chmod 700 /home/alice
  mkdir -p /var/lib/kaphub/home /etc/kaptest
  cd /etc/kaptest
  openssl req -x509 -newkey rsa:2048 -nodes -keyout hub.key -out hub.pem -days 2 -subj /CN=hub -addext subjectAltName=IP:127.0.0.1 2>/dev/null
  openssl x509 -in hub.pem -outform der | sha256sum | cut -d' ' -f1 > hub.fp
  cat > /root/seed.py <<'EOF'
from pathlib import Path
from kaptyorka.store import Store
from kaptyorka.manager import ManagerStore
s = Store(Path("/var/lib/kaphub/hub.db")); ManagerStore(s).set_enabled(False, "test")
root = s.person_create("root", "admin"); alice = s.person_create("alice")
s.project_register("proj-a", by="root"); s.grant_project("proj-a", alice, "write", "root")
fp = open("/etc/kaptest/hub.fp").read().strip()
for name in ("invite", "invite-2", "invite-3"):
    Path(f"/etc/kaptest/{name}").write_text(s.invite_create(alice, kind="personal", by="root"))
    Path(f"/etc/kaptest/link-{name}").write_text(f"https://127.0.0.1:8443/i/{open(f'/etc/kaptest/{name}').read()}#sha256={fp}")
Path("/etc/kaptest/team-invite").write_text(s.invite_create(alice, by="root"))
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
  cat > /root/q.py <<'EOF'
import sqlite3, sys
d = sqlite3.connect("file:/var/lib/kaphub/hub.db?mode=ro", uri=True)
for r in d.execute(sys.argv[1]): print("|".join(map(str, r)))
EOF
  # the README's lines with a link and a key put in: /root/boot <link file> <key file> [extra installer options]
  cat > /root/boot <<'EOF'
#!/bin/bash
link=$(cat "$1"); key=$(cat "$2"); shift 2
python3 -I -c 'import sys; link, key, extra = sys.argv[1:4]
cmds = [c for c in open("/etc/kaptest/readme.cmds").read().splitlines() if c.strip()]
out = [c.replace("<link>", link).replace("ssh-ed25519 REPLACE-WITH-THE-RELEASE-PUBLIC-KEY", key) for c in cmds]
out[-1] += " " + extra
open("/root/boot.run", "w").write("\n".join(out) + "\n")' "$link" "$key" "$*"
rm -rf /home/alice/Downloads/boot && mkdir -p /home/alice/Downloads/boot && cd /home/alice/Downloads/boot && bash /root/boot.run
EOF
  chmod 700 /root/boot
  t "S6-0 setup" "ssh-keygen, certificate, the signed releases built on the host" \
    're:^rc=0 /usr/bin/ssh-keygen [0-9a-f]{64} bootchanged changed changedpyz otherkey unsigned v50.0.1 v50.0.2 v50.0.3 wrongfile $' \
    "command -v ssh-keygen | tr '\n' ' '; tr -d '\n' < /etc/kaptest/hub.fp; echo -n ' '; ls /root/rel | tr '\n' ' '"
  ;;

hubvenv)
  python3 -m venv /opt/hubvenv && timeout 360 /opt/hubvenv/bin/pip install -q /root/hubwheel/*.whl 'mcp==2.3.0' >/root/pip.log 2>&1
  t "S6-1 the hub's packages" "importable" 're:^rc=0 ok$' "$HUBPY -c 'import mcp, uvicorn, kaptyorka.hub; print(\"ok\")'"
  ;;

hub)
  $HUBPY /root/seed.py
  systemd-run --quiet --unit=kaptest-hub -p RuntimeMaxSec=1500 -p KillMode=control-group -p TimeoutStopSec=5 \
    -p Environment=KAPTYORKA_HOME=/var/lib/kaphub/home /opt/hubvenv/bin/python3 /root/hub.py
  wait_for 30 'curl -sk -o /dev/null https://127.0.0.1:8443/'
  serve v$V1
  I=$(cat /etc/kaptest/invite)
  t "H-1 the hub serves the release's three files with a valid personal invite code" "200 x3, the signed files" \
    "re:^rc=0 200 200 200 $(sha v$V1)\$" \
    "for f in kaptyorka-secure-setup.pyz release.manifest release.manifest.sig; do curl -sk -o /tmp/h-\$f -w '%{http_code} ' https://127.0.0.1:8443/i/$I/release/\$f; done; sha256sum /tmp/h-kaptyorka-secure-setup.pyz | cut -c1-12"
  t "H-2 not without one: a team invite, a made-up code, no token, another file name" "404 404 401 404" \
    're:^rc=0 404 404 401 404 $' \
    "for u in /i/$(cat /etc/kaptest/team-invite)/release/release.manifest /i/AAAAAAAAAAAAAAAAAAAAAA/release/release.manifest /box/v1/release/release.manifest /i/$I/release/hub.db; do curl -sk -o /dev/null -w '%{http_code} ' https://127.0.0.1:8443\$u; done"
  t "H-3 no upload route: PUT and POST to the release paths change nothing" "refused; the file unchanged" \
    "re:^rc=0 (40[0-9] ){4}$(sha v$V1)\$" \
    "for m in PUT POST; do for u in /i/$I/release/kaptyorka-secure-setup.pyz /box/v1/release/kaptyorka-secure-setup.pyz; do curl -sk -o /dev/null -w '%{http_code} ' -X \$m --data-binary evil https://127.0.0.1:8443\$u; done; done; sha256sum $RELS/current/kaptyorka-secure-setup.pyz | cut -c1-12"
  ;;

boot)
  OPTS="--person alice --name alice-laptop --runtime-max 300s --tools none"
  # unused <file with a code>: 1 while that invite is not used
  printf '#!/bin/bash\n%s /root/q.py "SELECT used_at IS NULL FROM invites WHERE code='"'"'$(cat /etc/kaptest/$1)'"'"'"\n' "$HUBPY" > /root/unused
  chmod 700 /root/unused
  t "B-0 the README's install lines: link, key, download from the hub, signers, then verify && hash && install" \
    "five lines as written" 're:^rc=0 LINK=[^ ]+ KEY=[^ ]+ U=[^ ]+ echo ssh-keygen ok$' \
    "awk '{printf \"%s \", \$1}' /etc/kaptest/readme.cmds; tail -1 /etc/kaptest/readme.cmds | grep -q '^ssh-keygen -Y verify -f release.signers -I kaptyorka-release -n kaptyorka-release -s release.manifest.sig < release.manifest && grep -qx .* && sudo python3 -I kaptyorka-secure-setup.pyz install --personal \"\$LINK\"\$' && echo ok"
  serve bootchanged
  TL=120 t "B-1 a hub serving a changed installer: the signature is good, the hash is not; nothing runs, the link is not used" \
    "Good signature; no install; unused=1" \
    're:^rc=0 .*Good "kaptyorka-release" signature.* exit=1 installed=no unused=1$' \
    "/root/boot /etc/kaptest/link-invite /etc/kaptest/testkey.line $OPTS; echo \" exit=\$? installed=\$(test -e /etc/kaptyorka && echo yes || echo no) unused=\$(/root/unused invite)\""
  serve v$V1
  TL=120 t "B-2 a key that is not the release key (another one pasted): refused before anything runs" \
    "signature not good; no install; unused=1" \
    're:^rc=0 .*(Could not verify signature|Signature verification failed).* exit=255 installed=no unused=1$' \
    "/root/boot /etc/kaptest/link-invite /etc/kaptest/otherkey.line $OPTS; echo \" exit=\$? installed=\$(test -e /etc/kaptyorka && echo yes || echo no) unused=\$(/root/unused invite)\""
  TL=240 t "B-3 the README's lines with the release key: the signed $V1 installs from the hub with the link" \
    "Good signature; joined; self-check passes; Done" \
    're:^rc=0 exit=0.Good "kaptyorka-release" signature.*Joined: this computer is alice-laptop.*Installing kaptyorka 50\.0\.1 for alice.*Self-check: all walls in place.*Done\. This computer is alice-laptop' \
    "/root/boot /etc/kaptest/link-invite /etc/kaptest/testkey.line $OPTS > /root/b3.log 2>&1; echo \"exit=\$?\"; grep -oE '(Good \"kaptyorka-release\" signature|Joined: this computer is alice-laptop|Installing kaptyorka [0-9.]+ for alice|Self-check: all walls in place|Done\. This computer is alice-laptop)' /root/b3.log"
  t "B-4 the release key is pinned root-owned in /etc/kaptyorka; the update timer is on" \
    "the test key, 644 root; timer enabled+active" \
    "re:^rc=0 kaptyorka-release namespaces=\"kaptyorka-release\" $(cat /etc/kaptest/testkey.line | sed 's/[+/]/./g') 644 root enabled active\$" \
    "tr -d '\n' < /etc/kaptyorka/release-key; echo -n ' '; stat -c '%a %U' /etc/kaptyorka/release-key | tr '\n' ' '; systemctl is-enabled kaptyorka-update.timer | tr '\n' ' '; systemctl is-active kaptyorka-update.timer"
  t "B-5 the used link downloads nothing any more" "404" 're:^rc=0 404$' \
    "curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1:8443/i/$(cat /etc/kaptest/invite)/release/release.manifest"
  t "B-6 the installed copy is the signed $V1 installer" "same sha" "re:^rc=0 $(sha v$V1)\$" "sha256sum $L/installer | cut -c1-12"
  t "B-7 hubchat add proj-a as alice" "a team folder" 're:proj-a is now a team folder' "runuser -u alice -- bash -c 'hubchat add ~/team/proj-a' 2>&1"
  ;;

update)
  fresh() { python3 -I -c 'import json; p="/etc/kaptyorka/update.json"
try: s = json.load(open(p))
except (OSError, ValueError): s = {}
s.pop("checked", None); json.dump(s, open(p, "w"))'; }
  check() { fresh; $UPD run --now >/dev/null 2>&1; lap; }
  export -f fresh check
  S1=$(sha v$V1) S2=$(sha v$V2)
  for c in unsigned otherkey changed changedpyz wrongfile; do
    case $c in
      unsigned) why="refused: the release is not signed";;
      otherkey|changed) why="refused: the release's signature is not good for the release key";;
      changedpyz|wrongfile) why="refused: the installer is not the one the signed manifest names";;
    esac
    serve $c
    TL=90 t "U-$c the hub offers a bad $V2 ($c): refused, nothing waits, nothing installed" "$why" \
      "re:^rc=0 version=$V1 installer=$S1 staged=0 result=$why" "check"
  done
  serve v$V1
  TL=90 t "U-same the same version, signed: nothing to do" "up to date" "re:^rc=0 version=$V1 installer=$S1 staged=0 result=up to date\$" "check"
  t "U-list hubchat list shows the version and the last check" "kaptyorka 50.0.1; last update check: <time> (up to date)" \
    're:kaptyorka 50\.0\.1; last update check: 20[0-9-]+ [0-9:]+ \(up to date\)' "runuser -u alice -- hubchat list 2>&1"
  # a good 50.0.2 while a team chat runs (a unit named as enter names a chat's): it waits, verified
  serve v$V2
  systemd-run --quiet --unit=kapbox-alice-chat-claude-0000beef -p RuntimeMaxSec=200 /bin/sleep 190
  TL=120 t "U-wait a good $V2 while a team chat runs: downloaded, verified, waiting; nothing installed" \
    "waiting; version 50.0.1" "re:^rc=0 version=$V1 installer=$S1 staged=3 result=$V2 is waiting until no team chat runs\$" "check"
  t "U-wait-list hubchat list says it is waiting" "Update 50.0.2 is waiting" \
    're:Update 50\.0\.2 is waiting: it installs by itself once no team chat runs' "runuser -u alice -- hubchat list 2>&1"
  TL=90 t "U-wait-enter a team chat's start says it too" "enter: kaptyorka 50.0.2 is downloaded and verified" \
    're:enter: kaptyorka 50\.0\.2 is downloaded and verified; it installs by itself once no team chat runs' \
    "runuser -u alice -- bash -c 'cd ~/team/proj-a && $L/launch git status' 2>&1"
  TL=60 t "U-wait-perm the waiting files are root's only" "700 root; 600 files" \
    're:^rc=0 700 root (600 ){3}$' "stat -c '%a %U' $L/update | tr '\n' ' '; stat -c '%a' $L/update/staged-* | tr '\n' ' '"
  systemctl stop kapbox-alice-chat-claude-0000beef.service
  rm -rf "$RELS/x" "$RELS/current"   # the hub offers nothing now: the waiting files are enough
  TL=600 t "U-good no team chat runs: the timer's own service installs $V2 with its repair; self-check passed" \
    "installed 50.0.2 (was 50.0.1)" \
    "re:^rc=0 version=$V2 installer=$S2 staged=0 result=installed $V2 \\(was $V1\\); self-check passed\$" \
    "timeout 590 systemctl start kaptyorka-update.service; lap"
  t "U-good-2 the new code is in place, the old installer kept for a rollback, relay.conf says $V2" \
    "kap_update.py of 50.0.2; previous = 50.0.1's installer; version=50.0.2" \
    "re:^rc=0 $(sha v$V1) version=$V2 nomarker\$" \
    "sha256sum $L/update/previous.pyz | cut -c1-12 | tr '\n' ' '; grep '^version=' /etc/kaptyorka/relay.conf | tr '\n' ' '; test -e /etc/kaptyorka/install-in-progress || echo nomarker"
  t "U-good-list hubchat list: the new version, walls in place" "kaptyorka 50.0.2; all walls in place" \
    're:kaptyorka 50\.0\.2; last update check:.*Self-check: all walls in place' "runuser -u alice -- hubchat list 2>&1"
  serve v$V1
  TL=90 t "U-downgrade the hub offers the older signed $V1 now: refused" "older than 50.0.2: refused" \
    "re:^rc=0 version=$V2 installer=$S2 staged=0 result=refused: the hub offers $V1, older than $V2\$" "check"
  serve v$V2
  TL=90 t "U-same-2 the same version again: nothing to do" "up to date" "re:^rc=0 version=$V2 installer=$S2 staged=0 result=up to date\$" "check"
  # 50.0.3, signed, but its wallcheck fails: the self-check after its repair fails, the kept installer puts 50.0.2 back
  serve v$V3
  TL=600 t "U-rollback a signed $V3 whose self-check fails: rolled back to $V2" "failed; rolled back to 50.0.2" \
    "re:^rc=0 version=$V2 installer=$S2 staged=0 result=$V3 failed \\(.*\\); rolled back to $V2\$" "fresh; timeout 590 $UPD run --now >/dev/null 2>&1; lap"
  t "U-rollback-2 after the rollback every wall is in place again (50.0.2's own wallcheck)" "all walls in place" \
    're:kaptyorka 50\.0\.2; .*Self-check: all walls in place' "runuser -u alice -- hubchat list 2>&1"
  TL=90 t "U-rollback-3 $V3 is never tried again" "failed its self-check here before" \
    "re:^rc=0 version=$V2 installer=$S2 staged=0 result=$V3 failed its self-check here before; waiting for a newer release\$" "check"
  t "U-key the pinned key is still the test key (no installer replaced it)" "unchanged" \
    "re:^rc=0 kaptyorka-release namespaces=\"kaptyorka-release\" $(cat /etc/kaptest/testkey.line | sed 's/[+/]/./g')\$" "cat /etc/kaptyorka/release-key"
  systemctl stop kaptest-hub.service
  TL=90 t "U-hubdown the hub is gone: the check says so, nothing changes" "cannot reach the hub" \
    "re:^rc=0 version=$V2 installer=$S2 staged=0 result=cannot reach the hub" "check"
  t "U-canary no personal canary in the box's view after all this" "none" 're:^rc=0 none$' \
    "grep -rl CANARY-PERSONAL /var/lib/kap-team /srv/team 2>/dev/null | head -3; echo none"
  ;;
esac
exit 0
