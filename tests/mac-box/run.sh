#!/bin/bash
# The macOS box, end to end, on a throw-away Mac (the GitHub macOS runners of
# .github/workflows/mac-box.yml). TEST VERSION of the Mac box: this is its real test.
#
#   sudo KAP_PY=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 tests/mac-box/run.sh
#
# It installs the box for the account that ran sudo (the runner) with a fake hub address, a fake
# device key and Claude token, and fake tools (fake-claude.sh as claude), plants the wall kit's
# canaries as that person, makes a team folder, starts the fake claude in it through the normal
# launcher and runs every attack from inside the box; then it breaks one wall at a time and checks
# that no chat starts; then hubchat remove and the installer's remove. Results: out/results.tsv.
# NEVER run it on a Mac someone uses: it changes accounts, sudo, pf and the person's home mode.
# Bash 3.2 compatible.
set -u
export PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C
HERE=$(cd "$(dirname "$0")" && pwd -P)
REPO=$(cd "$HERE/../.." && pwd -P)
OUT=$HERE/out
PY=${KAP_PY:?set KAP_PY to a root-owned Python 3.10+}
P=${SUDO_USER:?run with sudo from the runner account}
PH=/Users/$P
B=kap-$P
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 2; }
[ "$(uname -s)" = Darwin ] || { echo "macOS only" >&2; exit 2; }
[ "${CI:-}" = true ] || [ "${KAPCI_I_AM_A_THROWAWAY_MAC:-}" = yes ] || { echo "CI only (see the header)" >&2; exit 2; }
rm -rf "$OUT"; mkdir -p "$OUT"
RES=$OUT/results.tsv; LOG=$OUT/log.txt
: >"$RES"; : >"$LOG"; printf 'id\texpected\tactual\tresult\n' >>"$RES"
NP=0; NF=0; NS=0
SKIP=""   # set when the install failed: every later row is SKIP (it could not mean anything), not FAIL
FP=$(printf 'ab%.0s' $(seq 1 32))

tmo() { perl -e 'my $s=shift; my $p=fork(); if(!$p){ setpgrp(0,0); exec @ARGV; exit 127 }
  $SIG{ALRM}=sub{ kill "KILL", -$p; kill "KILL", $p; exit 124 }; alarm $s; waitpid($p,0); exit($?>>8)' "$@"; }
rec() { # id expected actual PASS|FAIL|INFO
  local a r=$4; a=$(printf '%s' "$3" | tr '\t\r\n' '   ' | tr -s ' ' | cut -c1-300)
  if [ -n "$SKIP" ] && [ "$r" != INFO ]; then r=SKIP; a="skipped: $SKIP"; fi
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$a" "$r" >>"$RES"
  case $r in PASS) NP=$((NP+1));; FAIL) NF=$((NF+1));; SKIP) NS=$((NS+1));; esac
  printf '  %-5s %s\n' "$r" "$1"
}
check() { # id expected cond(0/1) actual
  if [ "$3" = 0 ]; then rec "$1" "$2" "$4" PASS; else rec "$1" "$2" "$4" FAIL; fi
}
has() { printf '%s' "$1" | grep -Fq -- "$2"; }
as_person() { tmo "${2:-120}" sudo -n -u "$P" -H /bin/zsh -l -c "$1" </dev/null 2>&1; }   # fresh groups, the system shell files
log() { printf '\n==== %s\n%s\n' "$1" "$2" >>"$LOG"; }

echo "== facts"
rec META-OS "recorded" "$(sw_vers -productVersion) $(sw_vers -buildVersion) $(uname -m)" INFO
rec META-SIP "recorded" "$(csrutil status 2>&1 | head -1)" INFO
rec META-PF "recorded" "$(pfctl -s info 2>/dev/null | grep -i status | head -1)" INFO
rec META-PY "recorded" "$PY $("$PY" -V 2>&1)" INFO
rec META-HOME-MODE-BEFORE "recorded" "$(stat -f %Lp "$PH")" INFO

echo "== the installer file and the fake tools"
"$PY" -I "$REPO/src/kaptyorka/secure/kap_installer.py" build "$OUT/kaptyorka-secure-setup.pyz" >>"$LOG" 2>&1
check BUILD "installer built" $? "$(ls -l "$OUT/kaptyorka-secure-setup.pyz" 2>&1)"
mkdir -p "$OUT/tools/bin" "$OUT/secrets"
cp "$HERE/fake-claude.sh" "$OUT/tools/bin/claude"
printf '#!/bin/sh\necho "fake codex $*"\n' >"$OUT/tools/bin/codex"
chmod 755 "$OUT/tools/bin/claude" "$OUT/tools/bin/codex"
printf 'FAKECI-DEVICE-TOKEN-0123456789abcdef\n' >"$OUT/secrets/device"
printf 'sk-ant-oat01-FAKECI-claude-token-0123456789\n' >"$OUT/secrets/claude"
chmod 600 "$OUT/secrets/"*

echo "== the person's canaries (the wall kit's)"
plant() { mkdir -p "$(dirname "$1")"; printf 'secret: %s\n' "$2" >"$1"; chmod "$3" "$1"; }
plant "$PH/workspace/private-note.txt" KAPCI-PERSONAL-FILE-7f3a 644
plant "$PH/.gitconfig.kapci" x 644
[ -e "$PH/.gitconfig" ] && cp "$PH/.gitconfig" "$OUT/gitconfig.saved"
printf '[user]\n\tname = KAPCI-GITCONFIG-1b6c\n' >"$PH/.gitconfig"
plant "$PH/.ssh/id_fake" KAPCI-SSH-KEY-c0de 600
plant "$PH/.claude/.credentials.json" KAPCI-CLAUDE-CRED-b41d 600
plant "$PH/.claude/projects/-demo/chat.jsonl" KAPCI-CHAT-2e8f 644
plant "$PH/Library/Keychains/fake.keychain-db" KAPCI-KEYCHAIN-d00d 644
plant "$PH/KAPCI-DIR-LISTING-8d3e" x 644
mkdir -p "$PH/.sockdir"
chown -R "$P" "$PH/workspace" "$PH/.gitconfig" "$PH/.ssh" "$PH/.claude" "$PH/Library/Keychains/fake.keychain-db" \
  "$PH/KAPCI-DIR-LISTING-8d3e" "$PH/.sockdir" "$PH/.gitconfig.kapci"
printf 'KAPCI-TMP-5e0d\n' >/private/tmp/kapci-canary; chmod 644 /private/tmp/kapci-canary
printf 'KAPCI-VTMP-9a11\n' >/private/var/tmp/kapci-canary; chmod 644 /private/var/tmp/kapci-canary
printf 'KAPCI-SHARED-77f1\n' >/Users/Shared/kapci-canary; chmod 644 /Users/Shared/kapci-canary
cat >"$OUT/listen.pl" <<'EOF'
use strict; use IO::Socket::UNIX; use IO::Socket::INET;
my ($kind, $where, $msg) = @ARGV; my $s;
if ($kind eq 'unix') { unlink $where; $s = IO::Socket::UNIX->new(Local => $where, Listen => 5) or die; chmod 0777, $where;
  while (my $c = $s->accept) { print $c "$msg\n"; close $c; } }
else { $s = IO::Socket::INET->new(LocalAddr => '127.0.0.1', LocalPort => $where, Listen => 5, ReuseAddr => 1) or die;
  while (my $c = $s->accept) { my $l = <$c>; print $c "HTTP/1.0 200 OK\r\n\r\n$msg\n"; close $c; } }
EOF
chmod 644 "$OUT/listen.pl"
bg() { ( sudo -n -u "$P" -H "$@" </dev/null >/dev/null 2>&1 & ); }
bg /usr/bin/perl "$OUT/listen.pl" tcp 38917 KAPCI-LOCALHOST-3c9d
bg /usr/bin/perl "$OUT/listen.pl" unix /private/tmp/kapci.sock KAPCI-SOCKET-TMP-91ce
bg /usr/bin/env KAPCI_ENV_SECRET=KAPCI-ENV-SECRET-5a77 /bin/sh -c "cd $PH; sleep 5400; echo KAPCI-ARG-SECRET-4d1e"
sudo -n -u "$P" -H /usr/bin/ssh-keygen -q -t ed25519 -N '' -C KAPCI-AGENTKEY-77aa -f "$PH/.ssh/id_kapci" >>"$LOG" 2>&1
sudo -n -u "$P" -H /usr/bin/ssh-agent -a "$PH/.sockdir/agent.sock" >>"$LOG" 2>&1
sudo -n -u "$P" -H /usr/bin/env SSH_AUTH_SOCK="$PH/.sockdir/agent.sock" /usr/bin/ssh-add "$PH/.ssh/id_kapci" >>"$LOG" 2>&1
printf 'KAPCI-CLIP-5f12' | sudo -n -u "$P" /usr/bin/pbcopy >>"$LOG" 2>&1
sleep 2
APID=$(pgrep -u "$P" -f KAPCI-ARG-SECRET | head -1)
rec META-PERSON-CANARY-PID "recorded" "${APID:-none}" INFO
check CONTROL-PERSON-READS-OWN "the person reads her own canary" \
  "$(as_person "cat ~/workspace/private-note.txt" | grep -q KAPCI-PERSONAL && echo 0 || echo 1)" "positive control"

echo "== install"
o=$(tmo 900 "$PY" -I "$OUT/kaptyorka-secure-setup.pyz" install --personal --person "$P" --name box-1 \
      --hub-ip 127.0.0.1 --hub-port 9 --hub-sha256 "$FP" --device-token-file "$OUT/secrets/device" \
      --claude-token-file "$OUT/secrets/claude" --tools none --tools-from "$OUT/tools" --python "$PY" 2>&1); rc=$?
log install "$o"
printf '%s\n' "$o" >"$OUT/install.txt"
check INSTALL "exit 0" "$([ $rc = 0 ] && echo 0 || echo 1)" "rc=$rc $(printf '%s' "$o" | grep -i 'kaptyorka-secure-setup:\|problem\|error' | head -5) $(printf '%s' "$o" | tail -3)"
check INSTALL-SAYS-TEST-VERSION "TEST VERSION printed" "$(has "$o" "TEST VERSION" && echo 0 || echo 1)" "-"
check INSTALL-SELF-CHECK "first-start canary self-check passed" "$(has "$o" "Self-check: all walls in place" && echo 0 || echo 1)" \
  "$(printf '%s' "$o" | grep -i -A12 'self-check' | head -14)"
if [ $rc != 0 ]; then
  echo ""
  echo "=================== THE INSTALL FAILED (exit $rc). Its whole output (also out/install.txt):"
  printf '%s\n' "$o"
  echo "=================== everything after the install is SKIP; only the cleanup (REMOVE) still runs"
  SKIP="the install failed (exit $rc): see INSTALL and out/install.txt"
fi
check HOME-700 "home 700 after install" "$([ "$(stat -f %Lp "$PH")" = 700 ] && echo 0 || echo 1)" "$(stat -f %Lp "$PH")"
# the fake claude is a script: the installer makes claude execute-only (a binary's guard on Linux); a script
# must also be readable to run
chmod 755 "$("$PY" -I -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' /opt/kap-tools/bin/claude)" 2>>"$LOG"
ls -laR /opt/kap-tools >>"$LOG" 2>&1
dscl . -read "/Users/$B" >>"$LOG" 2>&1
pfctl -a com.apple/kaptyorka -s rules >>"$LOG" 2>&1
for l in pf canary relay update tools-update "egress.$P"; do launchctl print "system/com.kaptyorka.$l" 2>&1 | grep -E 'state|pid' | head -2 >>"$LOG"; done
check BOX-USER-HIDDEN "IsHidden 1" "$(dscl . -read "/Users/$B" IsHidden 2>&1 | grep -q 1 && echo 0 || echo 1)" "-"
check BOX-NOT-ADMIN "not in admin or staff" "$(id -Gn "$B" | tr ' ' '\n' | grep -Eqx 'admin|staff|wheel' && echo 1 || echo 0)" "$(id -Gn "$B")"
check PF-ENABLED "pf on with the anchor" "$(pfctl -s info 2>/dev/null | grep -q 'Status: Enabled' && pfctl -a com.apple/kaptyorka -s rules 2>/dev/null | grep -q user && echo 0 || echo 1)" "-"
for l in relay canary "egress.$P"; do
  pid=$(launchctl print "system/com.kaptyorka.$l" 2>/dev/null | awk '/^\tpid = /{print $3; exit}')
  rec "DAEMON-$l-USER" "recorded" "pid=${pid:-none} user=$( [ -n "$pid" ] && ps -o user= -p "$pid")" INFO
done

echo "== a team folder, a chat in it"
o=$(as_person "mkdir -p ~/workspace/proj-a && echo hi > ~/workspace/proj-a/README.md && echo mine > ~/workspace/proj-a/person-made.txt && hubchat add ~/workspace/proj-a"); rc=$?
log "hubchat add" "$o"
T=/Users/Shared/kaptyorka/$P/proj-a
check HUBCHAT-ADD "exit 0, moved, shortcut left" "$([ $rc = 0 ] && [ -d "$T" ] && [ -L "$PH/workspace/proj-a" ] && echo 0 || echo 1)" \
  "rc=$rc $(ls -lde "$T" 2>&1 | head -3)"
printf '%s\n' "${APID:-1}" >"$T/.kapci-person-pid"; chmod 644 "$T/.kapci-person-pid"
check TEAM-OWNER "the box owns it, group the share group" "$([ "$(stat -f '%Su:%Sg' "$T")" = "$B:kapshare-$P" ] && echo 0 || echo 1)" "$(stat -f '%Su:%Sg %Sp' "$T")"
o=$(as_person "cd ~/workspace/proj-a && claude hello"); rc=$?
log "claude hello" "$o"
check CHAT-STARTS "the fake claude runs as the box in the team folder" \
  "$(has "$o" "FAKE-CLAUDE-HELLO uid=$B pwd=$T" && echo 0 || echo 1)" "rc=$rc $o"
check CHAT-TOKEN "the Claude token came on its descriptor" "$(has "$o" TOKEN-OK && echo 0 || echo 1)" "$o"
o=$(as_person "cd ~/workspace/proj-a && export SSH_AUTH_SOCK=$PH/.sockdir/agent.sock KAPCI_ENVLEAK=KAPCI-ENVLEAK-1a2b && claude attacks" 300)
log "claude attacks" "$o"
check ATTACKS-RAN "the attack suite ran to its end" "$(has "$o" ATTACKS-DONE && echo 0 || echo 1)" "$(printf '%s' "$o" | tail -3)"
printf '%s\n' "$o" | grep '^RESULT ' | while read -r _ id res detail; do
  printf '%s\t%s\t%s\t%s\n' "IN-$id" "from inside the box" "$detail" "$res" >>"$RES"
done
n_fail=$(printf '%s\n' "$o" | grep -c '^RESULT .* FAIL')
n_pass=$(printf '%s\n' "$o" | grep -c '^RESULT .* PASS')
NP=$((NP+n_pass)); NF=$((NF+n_fail))
printf '  inside: %s passed, %s failed\n' "$n_pass" "$n_fail"
if [ -n "$SKIP" ]; then NP=$((NP-n_pass)); NF=$((NF-n_fail)); fi

echo "== the box's OpenCode port, from outside: nobody but the box reaches it"
Q=$(cat "/etc/kaptyorka/boxes/$P/opencode-port" 2>/dev/null)
rec META-OC-PORT "recorded" "${Q:-none}" INFO
( as_person "cd ~/workspace/proj-a && claude oc-listen" 90 >"$OUT/oc-listen.txt" 2>&1 ) &
OCJOB=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do grep -q OC-LISTENING "$OUT/oc-listen.txt" 2>/dev/null && break; sleep 1; done
check OC-LISTENS "the box listens on its OpenCode port (positive control)" \
  "$(grep -q OC-LISTENING "$OUT/oc-listen.txt" 2>/dev/null && echo 0 || echo 1)" "$(head -c 300 "$OUT/oc-listen.txt" 2>/dev/null)"
OCUP=$(grep -q OC-LISTENING "$OUT/oc-listen.txt" 2>/dev/null && echo 1 || echo 0)
oc_blocked() { # id expected output: a pass means something only while the box's listener is up
  if [ "$OCUP" = 1 ]; then check "$1" "$2" "$(has "$3" KAPCI-OC-BOX && echo 1 || echo 0)" "$3"
  else rec "$1" "$2" "not tried: the box's listener did not start. $3" INFO; fi
}
probe() { /usr/bin/perl -MIO::Socket::INET -e 'my $c = IO::Socket::INET->new(PeerAddr=>"127.0.0.1", PeerPort=>$ARGV[0], Timeout=>4) or die "refused: $!\n"; $c->timeout(4); my $l = <$c>; print defined $l ? $l : "no answer\n"' "${Q:-1}" 2>&1; }
o=$(sudo -n -u "$P" /usr/bin/perl -MIO::Socket::INET -e 'my $c = IO::Socket::INET->new(PeerAddr=>"127.0.0.1", PeerPort=>$ARGV[0], Timeout=>4) or die "refused: $!\n"; my $l = <$c>; print defined $l ? $l : "no answer\n"' "${Q:-1}" 2>&1)
oc_blocked OC-PERSON-BLOCKED "the person cannot reach the box's OpenCode port" "$o"
o=$(sudo -n -u nobody /usr/bin/perl -MIO::Socket::INET -e 'my $c = IO::Socket::INET->new(PeerAddr=>"127.0.0.1", PeerPort=>$ARGV[0], Timeout=>4) or die "refused: $!\n"; my $l = <$c>; print defined $l ? $l : "no answer\n"' "${Q:-1}" 2>&1)
oc_blocked OC-OTHER-USER-BLOCKED "another user (nobody) cannot reach it" "$o"
o=$(probe)
oc_blocked OC-ROOT-BLOCKED "not even root's own connection gets through pf" "$o"
IP=$(ipconfig getifaddr en0 2>/dev/null)
o=$( [ -n "$IP" ] && sudo -n -u "$P" /usr/bin/perl -MIO::Socket::INET -e 'my $c = IO::Socket::INET->new(PeerAddr=>$ARGV[1], PeerPort=>$ARGV[0], Timeout=>4) or die "refused: $!\n"; my $l = <$c>; print defined $l ? $l : "no answer\n"' "${Q:-1}" "$IP" 2>&1)
oc_blocked OC-LAN-ADDR-BLOCKED "nothing reaches it on this Mac's own address (${IP:-no en0})" "$o"
wait "$OCJOB" 2>/dev/null
log "oc-listen" "$(cat "$OUT/oc-listen.txt" 2>/dev/null)"
o=$(as_person "cd ~/workspace/proj-a && echo new > new-by-person.txt && claude write && echo person >> ci-box-written.txt && echo PERSON-APPEND-OK && cat person-made.txt new-by-person.txt && ls -le new-by-person.txt")
log "write both ways" "$o"
check TEAM-BOTH-WRITE "the box and the person edit each other's files, also ones made later (inherited ACL)" \
  "$(has "$o" WRITE-OK && has "$o" APPEND-OK && has "$o" NEW-APPEND-OK && has "$o" PERSON-APPEND-OK && has "$o" "box line" && echo 0 || echo 1)" "$o"
o=$(as_person "cd ~/workspace/proj-a && git init -q . && git status --short | head -3; stat -f %Su .git")
log "git in the box" "$o"
check GIT-IN-BOX "git typed in a team folder runs as the box" "$(has "$o" "$B" && echo 0 || echo 1)" "$o"

echo "== the terminal"
o=$( { sleep 8; } | tmo 60 sudo -n -u "$P" -H /usr/bin/script -q /dev/null /bin/zsh -l -c \
       "cd ~/workspace/proj-a && claude tiocsti; claude escapes; IFS= read -r -t 3 line; echo \"GOT:[\$line]\"" 2>&1 | tr -d '\r')
log "pty" "$(printf '%s' "$o" | od -c | head -40)"
check PTY-TIOCSTI "typed text stays in the box's own terminal" "$(has "$o" 'GOT:[echo INJECTED' && echo 1 || echo 0)" "$o"
check PTY-ESCAPES-FILTERED "clipboard and title escapes do not reach the person" \
  "$(has "$o" ESC-END && ! has "$o" ']52;' && ! has "$o" 'KAPCI-TITLE' && echo 0 || echo 1)" "$(printf '%s' "$o" | grep -a ESC | cat -v)"

echo "== hubchat list"
o=$(as_person "hubchat list"); log "hubchat list" "$o"
check LIST-TEST-VERSION "says TEST VERSION" "$(has "$o" "TEST VERSION" && echo 0 || echo 1)" "-"
check LIST-SELF-CHECK "all walls in place" "$(has "$o" "Self-check: all walls in place" && echo 0 || echo 1)" "$(printf '%s' "$o" | grep -A6 Self-check)"

echo "== break one wall at a time: no chat may start"
hello_refused() { # id what-broke expect-in-output
  local o rc; o=$(as_person "cd ~/workspace/proj-a && claude hello"); rc=$?
  log "broken: $1" "$o"
  check "REFUSE-$1" "$2: refused, the tool never ran" \
    "$( ! has "$o" FAKE-CLAUDE-HELLO && [ $rc != 0 ] && has "$o" "$3" && echo 0 || echo 1)" "rc=$rc $(printf '%s' "$o" | head -4)"
}
hello_works() {
  local o; o=$(as_person "cd ~/workspace/proj-a && claude hello")
  check "AFTER-$1" "starts again once it is put back" "$(has "$o" FAKE-CLAUDE-HELLO && echo 0 || echo 1)" "$(printf '%s' "$o" | head -3)"
}
chmod 755 "$PH"; hello_refused HOME-OPEN "home 755" "home has mode 755"; chmod 700 "$PH"; hello_works HOME-OPEN
printf '(allow file-read* (subpath "/private/tmp"))\n' >/etc/kaptyorka/box-extra.sb
hello_refused PROFILE-TMP "the profile lets the box read /private/tmp" "wall M2 is missing"
printf '(allow network-outbound)\n' >/etc/kaptyorka/box-extra.sb
hello_refused PROFILE-NET "the profile lets the box reach any address" "wall M6 is missing"
printf '(allow process-info*)\n' >/etc/kaptyorka/box-extra.sb
hello_refused PROFILE-PROC "the profile lets the box see other processes" "wall M7 is missing"
printf '(allow mach-lookup (global-name "com.apple.pasteboard.1"))\n' >/etc/kaptyorka/box-extra.sb
o=$(as_person "cd ~/workspace/proj-a && claude hello"); log "broken: clipboard" "$o"
rec REFUSE-PROFILE-CLIPBOARD "clipboard service allowed: refused (when the session has one)" \
  "$(printf '%s' "$o" | head -3)" "$( ! has "$o" FAKE-CLAUDE-HELLO && has "$o" 'wall M8' && echo PASS || echo INFO)"
: >/etc/kaptyorka/box-extra.sb; hello_works PROFILE
dseditgroup -o edit -a "$B" -t user staff >>"$LOG" 2>&1
hello_refused BOX-IN-STAFF "the box user put in staff" "group staff"
dseditgroup -o edit -d "$B" -t user staff >>"$LOG" 2>&1; hello_works BOX-IN-STAFF
printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$B" >>"/etc/sudoers.d/kaptyorka-$P"
hello_refused SUDOERS "the sudo rule changed" "is not kaptyorka's rule"
sed -i '' '$d' "/etc/sudoers.d/kaptyorka-$P"; hello_works SUDOERS
launchctl bootout system/com.kaptyorka.relay >>"$LOG" 2>&1; mv /Library/LaunchDaemons/com.kaptyorka.relay.plist "$OUT/relay.plist.away"
hello_refused RELAY-GONE "the relay's daemon is gone" "com.kaptyorka.relay is not running"
mv "$OUT/relay.plist.away" /Library/LaunchDaemons/com.kaptyorka.relay.plist; hello_works RELAY-GONE
pfctl -a com.apple/kaptyorka -F rules >>"$LOG" 2>&1
o=$(as_person "cd ~/workspace/proj-a && claude hello")
check PF-SELF-REPAIR "pf's anchor flushed: root loads it again before the check" \
  "$(has "$o" FAKE-CLAUDE-HELLO && pfctl -a com.apple/kaptyorka -s rules 2>/dev/null | grep -q user && echo 0 || echo 1)" "$(printf '%s' "$o" | head -3)"
S=/private/var/lib/kap-team/$B/home/state/claude/settings.json
rm -f "$S"; printf '{"hooks": {}}' >"$S"; chown "$B" "$S"
o=$(as_person "cd ~/workspace/proj-a && claude hello")
check LOCKED-REPAIRED "a changed locked settings file is put back by root before the start" \
  "$(has "$o" FAKE-CLAUDE-HELLO && [ "$(stat -f %Su "$S")" = root ] && cmp -s "$S" /etc/kaptyorka/box/claude/user-settings.json && echo 0 || echo 1)" "$(stat -f '%Su %z' "$S")"

echo "== the real OpenCode, from its vendor, in the box"
o=$(as_person "hubchat update-tools opencode" 600); rc=$?
log "update-tools opencode" "$o"
check OC-REAL-INSTALL "hubchat update-tools opencode installs it from opencode.ai" "$([ $rc = 0 ] && echo 0 || echo 1)" "rc=$rc $(printf '%s' "$o" | tail -3)"
o=$(as_person "cd ~/workspace/proj-a && opencode --version" 120); rc=$?
log "opencode --version" "$o"
check OC-REAL-VERSION "opencode --version runs in the box" "$([ $rc = 0 ] && printf '%s' "$o" | grep -Eq '[0-9]+[.][0-9]+' && echo 0 || echo 1)" "rc=$rc $o"
o=$( { sleep 25; } | tmo 30 sudo -n -u "$P" -H /usr/bin/script -q /dev/null /bin/zsh -l -c "cd ~/workspace/proj-a && opencode" 2>&1 | tr -d '\r' | cat -v); rc=$?
log "opencode window" "$(printf '%s' "$o" | tail -c 4000)"
check OC-REAL-WINDOW "an OpenCode window starts in the box and stays up (no wall refusal, no bind error)" \
  "$( ! has "$o" "wallcheck: wall" && ! has "$o" "Operation not permitted" && ! has "$o" "EPERM" && ! has "$o" "box-exec:" && ! has "$o" "enter:" && echo 0 || echo 1)" \
  "rc=$rc $(printf '%s' "$o" | grep -a -i 'error\|wall\|denied\|permit\|enter:\|box-exec' | head -5)"

echo "== hubchat remove, then the installer's remove"
o=$(as_person "cd ~ && hubchat remove proj-a"); rc=$?
log "hubchat remove" "$o"
check HUBCHAT-REMOVE "back as the person's own folder, no ACL, hooks moved aside" \
  "$([ $rc = 0 ] && [ -d "$PH/workspace/proj-a" ] && [ ! -L "$PH/workspace/proj-a" ] && [ "$(stat -f %Su "$PH/workspace/proj-a/ci-box-written.txt")" = "$P" ] && ! ls -le "$PH/workspace/proj-a" | grep -q kapshare && echo 0 || echo 1)" \
  "rc=$rc $(ls -lde "$PH/workspace/proj-a" 2>&1 | head -2)"
SKIP=""   # the cleanup is checked even after a failed install: nothing may be left behind
o=$(tmo 600 "$PY" -I /usr/local/libexec/kaptyorka/installer remove --yes --delete-box-home 2>&1); rc=$?
[ -f /usr/local/libexec/kaptyorka/installer ] || { o="$o $(tmo 600 "$PY" -I "$OUT/kaptyorka-secure-setup.pyz" remove --yes --delete-box-home 2>&1)"; rc=$?; }
log remove "$o"
left=""
dscl . -read "/Users/$B" >/dev/null 2>&1 && left="$left user"
for u in _kaptyorka _kapproxy _kaptools; do dscl . -read "/Users/$u" >/dev/null 2>&1 && left="$left $u"; done
for p in /etc/kaptyorka /opt/kaptyorka /opt/kap-tools /usr/local/libexec/kaptyorka "/etc/sudoers.d/kaptyorka-$P" \
         /Library/LaunchDaemons/com.kaptyorka.relay.plist /Library/LaunchDaemons/com.kaptyorka.tools-update.plist \
         /usr/local/bin/hubchat; do [ -e "$p" ] && left="$left $p"; done
pfctl -a com.apple/kaptyorka -s rules 2>/dev/null | grep -q . && left="$left pf-anchor"
grep -q kaptyorka /etc/zshenv 2>/dev/null && left="$left zshenv"
launchctl print system/com.kaptyorka.relay >/dev/null 2>&1 && left="$left relay-daemon"
check REMOVE "everything it made is gone" "$([ $rc = 0 ] && [ -z "$left" ] && echo 0 || echo 1)" "rc=$rc left:$left"

pkill -u "$P" -f KAPCI- >/dev/null 2>&1; pkill -u "$P" ssh-agent >/dev/null 2>&1
[ -f "$OUT/gitconfig.saved" ] && cp "$OUT/gitconfig.saved" "$PH/.gitconfig"
rm -f "$OUT/secrets/"*
chmod -R a+rX "$OUT"
echo ""
echo "Results: $NP passed, $NF failed, $NS skipped. $RES"
[ "$NF" = 0 ]
