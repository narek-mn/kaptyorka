#!/bin/bash
# mac-tests.sh: macOS wall tests for the secure kaptyorka design (06-macos-research.md, 07 section 10 and 12.6 MAC rows).
#
# Run on a Mac, from an admin account:   sudo ./mac-tests.sh
#
# It makes two throwaway users (kaptest-box = the "box", kaptest-alice = a fake "person"),
# plants fake secrets ("canaries") in kaptest-alice's home, applies each wall, then runs every
# attack as kaptest-box and writes mac-results.tsv next to this script. Everything it makes is
# removed at the end (also on Ctrl-C). It never reads or changes the real user's files or settings.
# The only shared thing it touches is the clipboard, and only if you say yes (it puts it back).
#
# Written without access to a Mac: syntax-checked only. Bash 3.2 compatible (the macOS bash).
#
#   sudo ./mac-tests.sh             run the tests (asks YES first)
#   sudo ./mac-tests.sh --cleanup   remove what a stopped run left behind (only things the kit marked as its own)
# Optional: KAPTEST_NO_INTERNET=1 skips the four tests that reach example.com / 1.1.1.1.

# Start again with an empty environment. macOS sudo keeps some of the real user's variables
# (SSH_AUTH_SOCK, MAIL, HOME on some versions); none of them may reach the test users or the result file.
if [ "${KAPTEST_CLEANENV:-}" != 1 ]; then
  exec /usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin HOME=/var/root USER=root LOGNAME=root SHELL=/bin/sh \
    LC_ALL=C TERM="${TERM:-xterm}" KAPTEST_CLEANENV=1 SUDO_USER="${SUDO_USER:-}" \
    KAPTEST_YES="${KAPTEST_YES:-}" KAPTEST_CLIPBOARD="${KAPTEST_CLIPBOARD:-}" KAPTEST_NO_INTERNET="${KAPTEST_NO_INTERNET:-}" \
    /bin/bash "$0" "$@"
fi

set -u
umask 022
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
export LC_ALL=C
MODE=run
case "${1:-}" in "") ;; --cleanup) MODE=cleanup;; *) printf 'usage: sudo %s [--cleanup]\n' "$0" >&2; exit 2;; esac

BOX=kaptest-box
ALICE=kaptest-alice
SHARE=kaptest-share
KIT=/Library/Kaptest
SUDOERS=/etc/sudoers.d/kaptest-box
TEAMROOT=/Users/Shared/kaptest-team
OUTSIDE=/Users/Shared/kaptest-outside-canary
TMPCANARY=/private/tmp/kaptest-canary-tmp
VTMPCANARY=/private/var/tmp/kaptest-canary-vtmp
TMPSOCK=/private/tmp/kaptest-alice.sock
ANCHOR=com.apple/kaptest
AHOME=/Users/$ALICE
BHOME=/Users/$BOX
MARK="kaptest kit test record (safe to delete)"   # RealName of every record the kit makes
DIRMARK=.kaptest-kit                               # file inside every folder the kit makes
CLIPCANARY=CANARY-CLIPBOARD-5f12
MINID=7700                                         # the kit only ever uses ids from here up
HERE=$(cd "$(dirname "$0")" 2>/dev/null && pwd -P) || HERE=""
OUT=$HERE/mac-results.tsv
LOG=$HERE/mac-log.txt
[ "$MODE" = cleanup ] && OUT=$HERE/mac-cleanup.tsv
T=30                      # default seconds for one test command
DENYRE='ermission denied|not permitted|Operation not|denied|refused|[Cc]ould not|Couldn.t|[Ff]ailed|timed out|TIMEOUT|unreachable|No route|cannot|Cannot|fatal|rror|password|not allowed|[Ss]orry'

NP=0; NF=0; CLEANED=0; INTERRUPTED=0; PFTOKEN=""; PFBEFORE=""; PFUSED=0; CLIP=0; NETOK=0; GUARD=0; REDACT=0
APID=""; LPORT=""; PPORT=""; PROFILE=none; BOXTMP=""; AUID=""; BUID=""

# ---------------------------------------------------------------- small tools

# tmo SECONDS CMD...   run a command, kill it (and its children) after SECONDS; exit 124 on timeout.
tmo() {
  perl -e '
    use POSIX qw(setsid);
    my $s = shift; my $to = 0;
    my $pid = fork(); die "fork: $!" unless defined $pid;
    if (!$pid) { setsid(); exec @ARGV; exit 127; }
    # the child is in its own session, so Ctrl-C does not reach it: pass it on
    $SIG{INT} = $SIG{TERM} = $SIG{HUP} = sub { kill "KILL", -$pid; kill "KILL", $pid; exit 130; };
    $SIG{ALRM} = sub { $to = 1; kill "TERM", -$pid; kill "TERM", $pid; select(undef,undef,undef,0.5); kill "KILL", -$pid; kill "KILL", $pid; };
    alarm $s;
    my $r; do { $r = waitpid($pid, 0); } while ($r == -1 && $!{EINTR});
    alarm 0;
    exit 124 if $to;
    my $st = $?; exit(($st & 127) ? 128 + ($st & 127) : $st >> 8);
  ' "$@"
}

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# rec ID COMMAND EXPECTED ACTUAL RESULT   one line in the result file
rec() {
  local c a
  c=$(printf '%s' "$2" | tr '\t\r\n' '   ' | tr -s ' ' | cut -c1-240)
  a=$(printf '%s' "$4" | tr '\t\r\n' '   ' | tr -s ' ' | cut -c1-200)
  printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$c" "$3" "$a" "$5" >>"$OUT"
  case $5 in PASS) NP=$((NP+1));; FAIL) NF=$((NF+1));; esac
  printf '  %-5s %s\n' "$5" "$1"
}

# runners: each takes one shell command string and runs it as someone
run_root()  { tmo $T /bin/sh -c "$1"; }
run_alice() { tmo $T /usr/bin/sudo -n -u $ALICE -H /bin/sh -c "$1"; }
# the real path: the "person" enters the box through the sudo rule and box-start
run_abox()  { tmo $T /usr/bin/sudo -n -u $ALICE -H /usr/bin/sudo -n -u $BOX $KIT/libexec/box-start /bin/sh -c "$1"; }
# same, but with the person's secrets in the environment, to see what sudo lets through
run_abox_env() {
  tmo $T /usr/bin/sudo -n -u $ALICE -H /usr/bin/env SSH_AUTH_SOCK=/tmp/kaptest-fake-agent HOME=$AHOME \
    KAPTEST_ENVLEAK=CANARY-ENVLEAK-1a2b TERM=xterm \
    /usr/bin/sudo -n -u $BOX $KIT/libexec/box-start /bin/sh -c "$1"
}
run_nobody() { tmo $T /usr/bin/sudo -n -u nobody /bin/sh -c "$1"; }

# tc ID EXPECT CANARY RUNNER STRICT CMD
#   EXPECT leak : PASS when the output shows CANARY (a known gap, or a positive control)
#   EXPECT block: PASS when the output does not show CANARY; with STRICT=1 the command must also fail with a refusal
#   EXPECT info : only recorded
tc() {
  local id=$1 ex=$2 can=$3 runner=$4 strict=$5 cmd=$6 out rc has=0 res expd
  out=$("$runner" "$cmd" 2>&1 </dev/null); rc=$?
  [ "$rc" -eq 124 ] && out="$out [TIMEOUT]"
  printf '%s' "$out" | grep -Fq -- "$can" && has=1
  # clipboard: never write what is on the clipboard into the result file (the person may have copied something)
  [ "$REDACT" = 1 ] && out="(clipboard text not recorded) canary-seen=$has"
  case $ex in
    leak)  expd="shows $can"; if [ $has -eq 1 ]; then res=PASS; else res=FAIL; fi ;;
    block) expd="refused, no $can"
           if [ $has -eq 1 ]; then res=FAIL
           elif [ "$strict" = 1 ] && [ $rc -eq 0 ]; then res=FAIL
           elif [ "$strict" = 1 ] && [ -n "$out" ] && ! printf '%s' "$out" | grep -Eq "$DENYRE"; then res=FAIL
           else res=PASS; fi ;;
    *)     expd="recorded only"; res=INFO ;;
  esac
  rec "$id" "[$runner] $cmd" "$expd" "rc=$rc $out" "$res"
}

# ---------------------------------------------------------------- cleanup

# Every path the kit may create. Nothing outside this list is ever removed.
KITPATHS="$SUDOERS $KIT $AHOME $BHOME $TEAMROOT $OUTSIDE $TMPCANARY $VTMPCANARY $TMPSOCK /Users/Shared/kaptest-box-w /private/tmp/kaptest-box-w /private/var/tmp/kaptest-box-w"

pf_status() { tmo 10 /sbin/pfctl -s info 2>/dev/null | awk '/^Status:/{print $2; exit}'; }

# GUARD=0: a normal run (preflight proved none of the names existed, so everything with them is ours).
# GUARD=1: --cleanup after a stopped run: only remove records and folders that carry the kit's marks.
ours_rec() { [ "$GUARD" = 0 ] && return 0; tmo 20 /usr/bin/dscl . -read "$1" RealName 2>/dev/null | grep -Fq "$MARK"; }
del_path() {
  local p=$1
  case $p in
    /etc/sudoers.d/kaptest-box|/Library/Kaptest|/Users/kaptest-alice|/Users/kaptest-box|/Users/Shared/kaptest-*|/private/tmp/kaptest-*|/private/var/tmp/kaptest-*) ;;
    *) say "  not removing unexpected path: $p"; return 1 ;;
  esac
  if [ -L "$p" ]; then rm -f "$p"; return 0; fi          # a link: remove the link only, never what it points to
  [ -e "$p" ] || return 0
  if [ -d "$p" ]; then
    if [ "$GUARD" = 0 ] || [ -f "$p/$DIRMARK" ]; then rm -rf "$p"; else say "  left in place (no kit mark inside): $p"; fi
  else
    rm -f "$p"
  fi
}
uid_of() { # user -> its UniqueID, only if it is in the kit's range
  local u
  u=$(tmo 20 /usr/bin/dscl . -read "/Users/$1" UniqueID 2>/dev/null | awk '{print $2; exit}')
  case $u in ''|*[!0-9]*) return 1;; esac
  [ "$u" -ge "$MINID" ] && [ "$u" -lt 9000 ] && echo "$u"
}
restore_clip() { # put the saved text back, but only while our fake text is still there (never overwrite a new copy)
  local now
  [ -f "$KIT/tmp/clip.saved" ] && [ ! -L "$KIT/tmp/clip.saved" ] && [ -n "${SUDO_USER:-}" ] || return 0
  now=$(tmo 10 /usr/bin/sudo -n -u "$SUDO_USER" /usr/bin/pbpaste 2>/dev/null)
  [ "$now" = "$CLIPCANARY" ] && tmo 10 /usr/bin/sudo -n -u "$SUDO_USER" /usr/bin/pbcopy <"$KIT/tmp/clip.saved" >/dev/null 2>&1
  return 0
}

cleanup() {
  local u uid d tok now
  [ "$CLEANED" = 1 ] && return
  CLEANED=1
  trap '' INT TERM HUP
  say ""; say "Cleaning up ... (takes up to a minute; please wait)"
  # the sudo rule first: it is the most sensitive thing the kit adds
  if [ -f "$SUDOERS" ] && { [ "$GUARD" = 0 ] || grep -Fq "$KIT/libexec/box-start" "$SUDOERS"; }; then rm -f "$SUDOERS"; fi
  [ -L "$SUDOERS" ] && rm -f "$SUDOERS"
  # clipboard, while the saved copy still exists
  { [ "$CLIP" = 1 ] || [ "$GUARD" = 1 ]; } && restore_clip
  CLIP=0
  # stop everything the test users run (and any sudo around it)
  for u in $ALICE $BOX; do id "$u" >/dev/null 2>&1 && ours_rec "/Users/$u" && tmo 10 /usr/bin/pkill -9 -U "$u" >/dev/null 2>&1; done
  tmo 10 /usr/bin/pkill -9 -f "/Library/Kaptes[t]/" >/dev/null 2>&1
  sleep 1
  # pf: empty our anchor (rules only: "-F all" could also flush the global state table), give back our enable token,
  # and put pf back the way it was (on stays on, off goes back off)
  tmo 20 /sbin/pfctl -a "$ANCHOR" -F rules >/dev/null 2>&1
  tok=$PFTOKEN; [ -z "$tok" ] && [ -f "$KIT/pf.token" ] && tok=$(cat "$KIT/pf.token" 2>/dev/null)
  [ -z "$PFBEFORE" ] && [ -f "$KIT/pf.before" ] && PFBEFORE=$(cat "$KIT/pf.before" 2>/dev/null)
  [ "$PFUSED" = 0 ] && [ -f "$KIT/pf.used" ] && PFUSED=1
  case $tok in ''|*[!0-9]*) tok="";; esac
  [ -n "$tok" ] && tmo 20 /sbin/pfctl -X "$tok" >/dev/null 2>&1
  if [ "$PFUSED" = 1 ]; then
    now=$(pf_status)
    if [ "$PFBEFORE" = Enabled ] && [ "$now" = Disabled ]; then tmo 20 /sbin/pfctl -e >/dev/null 2>&1; fi
    if [ "$PFBEFORE" = Disabled ] && [ "$now" = Enabled ] && [ -z "$tok" ]; then tmo 20 /sbin/pfctl -d >/dev/null 2>&1; fi
  fi
  # the test users' own temp folders under /private/var/folders (macOS makes them; they outlive the user)
  for u in $ALICE $BOX; do
    ours_rec "/Users/$u" || continue
    uid=$(uid_of "$u") || continue
    tmo 30 /usr/bin/find /private/var/folders -mindepth 2 -maxdepth 2 -type d -user "$uid" 2>/dev/null |
    while IFS= read -r d; do
      case $d in /private/var/folders/?*/?*) [ -L "$d" ] || rm -rf "$d";; esac
    done
  done
  # users and groups (only the kit's own: see GUARD)
  for u in /Users/$BOX /Users/$ALICE /Groups/$BOX /Groups/$ALICE /Groups/$SHARE; do
    tmo 20 /usr/bin/dscl . -read "$u" >/dev/null 2>&1 || continue
    if ours_rec "$u"; then tmo 20 /usr/bin/dscl . -delete "$u" >/dev/null 2>&1; else say "  left in place (not made by the kit): $u"; fi
  done
  # sudo's own notes about the test users, if it made any
  rm -f /private/var/db/sudo/ts/$ALICE /private/var/db/sudo/ts/$BOX /private/var/db/sudo/lectured/$ALICE /private/var/db/sudo/lectured/$BOX
  # files and folders
  for d in $KITPATHS; do del_path "$d"; done
  tmo 20 /usr/bin/dscacheutil -flushcache >/dev/null 2>&1
}

verify_clean() {
  local o now
  o=$(tmo 20 /usr/bin/dscl . -read /Users/$BOX 2>&1); case $o in *"does not exist"*|*eDSRecordNotFound*) rec "Z-USER-BOX-GONE" "dscl . -read /Users/$BOX" "user removed" "removed" PASS;; *) rec "Z-USER-BOX-GONE" "dscl . -read /Users/$BOX" "user removed" "still there" FAIL;; esac
  o=$(tmo 20 /usr/bin/dscl . -read /Users/$ALICE 2>&1); case $o in *"does not exist"*|*eDSRecordNotFound*) rec "Z-USER-ALICE-GONE" "dscl . -read /Users/$ALICE" "user removed" "removed" PASS;; *) rec "Z-USER-ALICE-GONE" "dscl . -read /Users/$ALICE" "user removed" "still there" FAIL;; esac
  o=""; for d in $KITPATHS; do { [ -e "$d" ] || [ -L "$d" ]; } && o="$o $d"; done
  if [ -z "$o" ]; then rec "Z-FILES-GONE" "ls sudoers file, kit folder, homes, team area, canaries" "none left" "none left" PASS; else rec "Z-FILES-GONE" "ls leftovers" "none left" "$o" FAIL; fi
  o=$(tmo 20 /sbin/pfctl -a $ANCHOR -s rules 2>&1 | grep -v -e '^$' -e 'No ALTQ' -e 'ALTQ' | head -3)
  if [ -z "$o" ]; then rec "Z-PF-ANCHOR-EMPTY" "pfctl -a $ANCHOR -s rules" "no rules" "no rules" PASS; else rec "Z-PF-ANCHOR-EMPTY" "pfctl -a $ANCHOR -s rules" "no rules" "$o" FAIL; fi
  if [ "$PFUSED" = 1 ]; then
    now=$(pf_status)
    if [ "$now" = "$PFBEFORE" ]; then rec "Z-PF-STATE-RESTORED" "pfctl -s info (Status)" "same as before: $PFBEFORE" "$now" PASS
    else rec "Z-PF-STATE-RESTORED" "pfctl -s info (Status)" "same as before: $PFBEFORE" "${now:-unknown}" FAIL; fi
  fi
  o=$(/bin/ps -axo user=,comm= 2>/dev/null | grep -E '^kaptest' | head -3)
  if [ -z "$o" ]; then rec "Z-NO-PROCESSES" "ps -axo user=,comm= | grep kaptest" "none left" "none left" PASS; else rec "Z-NO-PROCESSES" "ps" "none left" "$o" FAIL; fi
  [ -f "$SUDOERS" ] && say "WARNING: $SUDOERS is still there."
}

finish() {
  cleanup
  verify_clean
  [ -n "${SUDO_USER:-}" ] && chown -h "$SUDO_USER" "$OUT" "$LOG" 2>/dev/null
  chmod -h 644 "$OUT" "$LOG" 2>/dev/null
}

on_signal() { INTERRUPTED=1; say ""; say "Interrupted. Cleaning up."; exit 130; }
on_exit() {
  if [ "$CLEANED" = 0 ]; then
    [ "$INTERRUPTED" = 1 ] && rec "Z-INTERRUPTED" "(Ctrl-C or error)" "run completes" "stopped early; results are partial" FAIL
    finish
  fi
  say ""
  say "Results: $NP passed, $NF failed (FAIL means: differs from what the research expected; it is a finding, not a mistake of yours)."
  say "Send this file back: $OUT"
}

# ---------------------------------------------------------------- preflight (nothing is changed here)

[ "$(uname -s)" = Darwin ] || die "this kit is for macOS only (this is $(uname -s))."
[ "$(id -u)" = 0 ] || die "run it with sudo:  sudo $0"
[ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ] || die "run it with sudo from your normal admin account (not from a root shell)."
for t in dscl dseditgroup pfctl visudo perl curl script sudo stat ps pkill pgrep dscacheutil find install getconf \
         lsof nc dig ping ssh-keygen ssh-agent ssh-add pbcopy pbpaste chflags sw_vers; do
  command -v $t >/dev/null 2>&1 || die "missing tool: $t"
done
# the result files: in a folder only you can write to, and never through a link
[ -n "$HERE" ] && [ -d "$HERE" ] || die "cannot find the folder this script is in."
[ -z "$(find "$HERE" -maxdepth 0 -perm -0002 2>/dev/null)" ] || die "$HERE can be changed by every user. Move the mac-kit folder into your own home folder (for example Downloads) and run it from there."
for f in "$OUT" "$LOG"; do
  [ -L "$f" ] && die "$f is a link. Delete it and run again."
  [ -e "$f" ] && [ ! -f "$f" ] && die "$f is not a normal file. Delete it and run again."
done
[ "$(tmo 10 dseditgroup -o checkmember -m "$SUDO_USER" admin 2>&1 | cut -c1-3)" = yes ] || die "$SUDO_USER is not an admin account."

if [ "$MODE" = cleanup ]; then
  # ------------------------------------------------------------ --cleanup: after a run that was stopped hard
  cat <<EOF

mac-tests.sh --cleanup will remove what an earlier, stopped run left on this Mac:
  - the test users and groups $BOX, $ALICE, $SHARE (only if the kit made them: it checks its mark)
  - the sudo rule file $SUDOERS (only if it is the kit's rule)
  - the pf rule set $ANCHOR, and it puts the firewall back on or off as it was
  - the kit's folders and files (all named kaptest / Kaptest; folders only if they carry the kit's mark)
Nothing else is touched.
EOF
  if [ "${KAPTEST_YES:-}" != 1 ]; then
    printf 'Type YES to continue: '; ans=""; read -r ans </dev/tty
    [ "$ans" = YES ] || die "stopped; nothing was changed."
  fi
  GUARD=1
  : >"$OUT"; printf 'id\tcommand\texpected\tactual\tresult\n' >>"$OUT"
  cleanup; verify_clean
  chown -h "$SUDO_USER" "$OUT" 2>/dev/null; chmod -h 644 "$OUT" 2>/dev/null
  say ""; say "Cleanup checks: $NP passed, $NF failed. Details: $OUT"
  exit 0
fi

grep -Eq '^[#@]includedir[[:space:]]+/(private/)?etc/sudoers\.d' /etc/sudoers 2>/dev/null || die "/etc/sudoers does not read /etc/sudoers.d here; the sudo test cannot run."
[ -d /etc/sudoers.d ] && [ ! -L /private/etc/sudoers.d ] || die "/etc/sudoers.d is missing; the sudo test cannot run."

left=""
for u in $BOX $ALICE; do
  tmo 20 dscl . -read /Users/$u >/dev/null 2>&1 && left="$left user:$u"
done
for g in $BOX $ALICE $SHARE; do
  tmo 20 dscl . -read /Groups/$g >/dev/null 2>&1 && left="$left group:$g"
done
for p in $KITPATHS; do
  { [ -e "$p" ] || [ -L "$p" ]; } && left="$left $p"
done
if tmo 20 pfctl -a $ANCHOR -s rules 2>/dev/null | grep -q .; then left="$left pf-anchor:$ANCHOR"; fi
[ -z "$left" ] || die "these already exist, so the kit will not start:$left
If an earlier run was stopped, run:  sudo $0 --cleanup"
command -v sandbox-exec >/dev/null 2>&1 && HAVE_SB=1 || HAVE_SB=0

cat <<EOF

mac-tests.sh will, on THIS Mac:
  - make two hidden test users ($BOX, $ALICE) and fake secrets in the second one's home
  - add one sudo rule file ($SUDOERS), one pf firewall rule set (anchor $ANCHOR) that only
    affects $BOX, and folders under /Library/Kaptest, /Users/Shared and /private/tmp (all named kaptest*)
  - run attacks as $BOX and write $OUT
  - contact example.com and 1.1.1.1 a few times to test internet blocking (nothing is uploaded;
    start with KAPTEST_NO_INTERNET=1 to skip this)
  - remove all of it at the end, also if you press Ctrl-C (press it once, then wait)
It does not read or change your own files or settings. Usually 10-20 minutes (not yet timed on a Mac).
EOF
if [ "${KAPTEST_YES:-}" != 1 ]; then
  printf 'Type YES to continue: '; ans=""; read -r ans </dev/tty
  [ "$ans" = YES ] || die "stopped; nothing was changed."
fi
if [ "${KAPTEST_CLIPBOARD:-}" = "" ]; then
  printf 'Clipboard test: this replaces your clipboard with a fake text for a few minutes and then puts your old TEXT back (a copied image or formatting would be lost; do not copy anything while it runs). Allow? [y/N] '
  ans=""; read -r ans </dev/tty
  case $ans in y|Y|yes|YES) KAPTEST_CLIPBOARD=yes;; *) KAPTEST_CLIPBOARD=no;; esac
fi

# ---------------------------------------------------------------- from here on, everything is undone by cleanup

rm -f "$OUT" "$LOG"; : >"$OUT"; : >"$LOG"
printf 'id\tcommand\texpected\tactual\tresult\n' >>"$OUT"
trap on_exit EXIT
trap on_signal INT TERM HUP
cd / || exit 1

must() { local d=$1; shift; if ! tmo 60 "$@" >>"$LOG" 2>&1; then say "SETUP FAILED: $d (see $LOG)"; exit 1; fi; }

say ""; say "== Setup =="
mkdir "$KIT" || exit 1
: >"$KIT/$DIRMARK"
mkdir -p "$KIT/libexec" "$KIT/tmp" || exit 1
chown -R root:wheel "$KIT"; chmod 755 "$KIT" "$KIT/libexec"; chmod 700 "$KIT/tmp"

# free ids: not used by any local user or group, nor by any directory (company network) account
USED=$( { tmo 20 dscl . -list /Users UniqueID; tmo 20 dscl . -list /Groups PrimaryGroupID; } | awk '{print $2}')
printf '%s\n' "$USED" | grep -qx 0 || { say "SETUP FAILED: could not list the existing user ids"; exit 1; }
id_free() {
  printf '%s\n' "$USED" | grep -qx "$1" && return 1
  [ -n "$(tmo 10 /usr/bin/dscacheutil -q user -a uid "$1" 2>/dev/null)" ] && return 1
  [ -n "$(tmo 10 /usr/bin/dscacheutil -q group -a gid "$1" 2>/dev/null)" ] && return 1
  return 0
}
BASE=$MINID
while :; do
  ok=1; i=0
  while [ $i -lt 4 ]; do id_free "$((BASE+i))" || ok=0; i=$((i+1)); done
  [ $ok = 1 ] && break
  BASE=$((BASE+10))
  [ $BASE -lt 8990 ] || { say "SETUP FAILED: no free user id between $MINID and 8999"; exit 1; }
done
AUID=$BASE; BUID=$((BASE+1)); SGID=$((BASE+2))

mkuser() { # name id
  local n=$1 id=$2
  must "group $n"  dscl . -create /Groups/$n
  must "gmark $n"  dscl . -create /Groups/$n RealName "$MARK"
  must "gid $n"    dscl . -create /Groups/$n PrimaryGroupID $id
  must "gpw $n"    dscl . -create /Groups/$n Password '*'
  must "user $n"   dscl . -create /Users/$n
  must "mark $n"   dscl . -create /Users/$n RealName "$MARK"
  must "shell $n"  dscl . -create /Users/$n UserShell /bin/zsh
  must "uid $n"    dscl . -create /Users/$n UniqueID $id
  must "pgid $n"   dscl . -create /Users/$n PrimaryGroupID $id
  must "home $n"   dscl . -create /Users/$n NFSHomeDirectory /Users/$n
  must "pw $n"     dscl . -create /Users/$n Password '*'
  must "hidden $n" dscl . -create /Users/$n IsHidden 1
  mkdir /Users/$n || { say "SETUP FAILED: /Users/$n"; exit 1; }
  : >/Users/$n/$DIRMARK
  chown $id:$id /Users/$n && chmod 755 /Users/$n
}
mkuser $ALICE $AUID
mkuser $BOX $BUID
chflags hidden $BHOME 2>>"$LOG"
must "share group" dseditgroup -o create -i $SGID -r "$MARK" $SHARE
must "alice in share" dseditgroup -o edit -a $ALICE -t user $SHARE
must "box in share" dseditgroup -o edit -a $BOX -t user $SHARE
tmo 20 dscacheutil -flushcache >/dev/null 2>&1; sleep 1
must "box usable" /usr/bin/sudo -n -u $BOX /usr/bin/id
must "alice usable" /usr/bin/sudo -n -u $ALICE /usr/bin/id

# canaries in the fake person's home: macOS default modes (directories 755, files 644), except where tools use 700/600
plant() { # path canary mode dirmode
  mkdir -p "$(dirname "$1")"
  printf 'secret: %s\n' "$2" >"$1"
  chmod "$3" "$1"
}
plant $AHOME/workspace/private-note.txt   CANARY-PERSONAL-FILE-7f3a 644
plant $AHOME/.gitconfig                   CANARY-GITCONFIG-1b6c 644
plant $AHOME/.claude/projects/-demo/chat.jsonl CANARY-PERSONAL-CHAT-2e8f 644
plant $AHOME/.claude/.credentials.json    CANARY-CLAUDE-CRED-b41d 600
plant $AHOME/Library/Keychains/login.keychain-db CANARY-KEYCHAIN-d00d 644
plant $AHOME/.ssh/id_fake                 CANARY-SSH-KEY-c0de 600
plant $AHOME/Documents/secret.txt         CANARY-DOCS-4a90 644
plant $AHOME/CANARY-DIR-LISTING-8d3e      x 644
chmod 700 $AHOME/.ssh $AHOME/Documents
mkdir -p $AHOME/.sockdir
chown -R $AUID:$AUID $AHOME
plant $BHOME/own.txt BOX-OWN-FILE-1a2b 644
chown -R $BUID:$BUID $BHOME; chmod 700 $BHOME
chflags hidden $BHOME 2>>"$LOG"
# root-made canaries in the shared places
printf 'CANARY-TMP-5e0d\n' >$TMPCANARY; chmod 644 $TMPCANARY
printf 'CANARY-VTMP-9a11\n' >$VTMPCANARY; chmod 644 $VTMPCANARY
mkdir -p /Users/Shared; printf 'CANARY-SHARED-77f1\n' >$OUTSIDE; chmod 644 $OUTSIDE
# the box's own temp folder (macOS makes it 0700 for each user)
BOXTMP=$(tmo 20 /usr/bin/sudo -n -u $BOX /usr/bin/getconf DARWIN_USER_TEMP_DIR 2>/dev/null)
[ -n "$BOXTMP" ] && BOXTMP=$(cd "$BOXTMP" 2>/dev/null && pwd -P)
# use it only if it really is the box's own folder under /private/var/folders (never write through a link)
case $BOXTMP in /private/var/folders/?*/?*/T) ;; *) BOXTMP="";; esac
if [ -n "$BOXTMP" ] && [ -d "$BOXTMP" ] && [ ! -L "$BOXTMP" ] && [ "$(stat -f %u "$BOXTMP")" = "$BUID" ] && [ ! -e "$BOXTMP/box-canary" ]; then
  printf 'CANARY-BOXTMP-3f4e\n' >"$BOXTMP/box-canary" && chown $BUID "$BOXTMP/box-canary"
else
  BOXTMP=""
fi

# helper scripts (root-owned, readable by everyone)
cat >$KIT/listen.pl <<'EOF'
use strict; use IO::Socket::UNIX; use IO::Socket::INET;
my ($kind, $where, $msg) = @ARGV;
my $s;
if ($kind eq 'unix') {
  unlink $where;
  $s = IO::Socket::UNIX->new(Local => $where, Listen => 5) or die "listen: $!";
  chmod 0777, $where;
  while (my $c = $s->accept) { print $c "$msg\n"; close $c; }
} else {
  $s = IO::Socket::INET->new(LocalAddr => '127.0.0.1', LocalPort => $where, Listen => 5, ReuseAddr => 1) or die "listen: $!";
  while (my $c = $s->accept) { my $l = <$c>; print $c "HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\n\r\n$msg\n"; close $c; }
}
EOF
cat >$KIT/sti.pl <<'EOF'
# types "echo INJECTED" into the terminal it is started from (TIOCSTI = 0x80017472 on macOS)
for my $c (split //, "echo INJECTED\n") {
  ioctl(STDIN, 0x80017472, $c) or do { print "TIOCSTI-FAIL: $!\n"; exit 1 };
}
print "TIOCSTI-DONE\n";
EOF
cat >$KIT/libexec/box-start <<'EOF'
#!/bin/sh
# test stand-in for the real launcher: leaves the environment exactly as sudo gave it
MODE=$(cat /Library/Kaptest/mode 2>/dev/null)
cd /Users/kaptest-box 2>/dev/null
if [ "$MODE" = seatbelt ]; then exec /usr/bin/sandbox-exec -f /Library/Kaptest/box.sb "$@"; fi
exec "$@"
EOF
chown -R root:wheel $KIT/libexec $KIT/listen.pl $KIT/sti.pl
chmod 755 $KIT/libexec/box-start; chmod 644 $KIT/listen.pl $KIT/sti.pl
set_mode() { printf '%s\n' "$1" >$KIT/mode; chmod 644 $KIT/mode; }
set_mode plain

# free ports
freeport() { local p=$1; while [ $p -lt $(( $1 + 200 )) ] && tmo 5 /usr/sbin/lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1; do p=$((p+1)); done; echo $p; }
LPORT=$(freeport 38917); PPORT=$(freeport $((LPORT+1)))

# listeners and a canary process, all run by the fake person
start_bg() { ( /usr/bin/sudo -n -u $ALICE -H "$@" </dev/null >/dev/null 2>&1 & ) ; }
start_bg /usr/bin/perl $KIT/listen.pl unix $AHOME/.sockdir/s.sock CANARY-SOCKET-HOME-6b02
start_bg /usr/bin/perl $KIT/listen.pl unix $TMPSOCK CANARY-SOCKET-TMP-91ce
start_bg /usr/bin/perl $KIT/listen.pl tcp $LPORT CANARY-LOCALHOST-3c9d
start_bg /usr/bin/perl $KIT/listen.pl tcp $PPORT PROXY-OK
start_bg /usr/bin/env CANARY_ENV_SECRET=CANARY-ENV-SECRET-5a77 /bin/sh -c "cd $AHOME; sleep 5400; echo CANARY-ARG-SECRET-4d1e"
must "alice ssh key" /usr/bin/sudo -n -u $ALICE -H /usr/bin/ssh-keygen -q -t ed25519 -N '' -C CANARY-AGENTKEY-77aa -f $AHOME/.ssh/id_agent
must "alice ssh-agent" /usr/bin/sudo -n -u $ALICE -H /usr/bin/ssh-agent -a $AHOME/.sockdir/agent.sock
tmo 10 /usr/bin/sudo -n -u $ALICE -H /bin/chmod 777 $AHOME/.sockdir/agent.sock >>"$LOG" 2>&1
must "alice ssh-add" /usr/bin/sudo -n -u $ALICE -H /usr/bin/env SSH_AUTH_SOCK=$AHOME/.sockdir/agent.sock /usr/bin/ssh-add $AHOME/.ssh/id_agent
i=0; while [ $i -lt 20 ]; do
  [ -S $AHOME/.sockdir/s.sock ] && [ -S $TMPSOCK ] && tmo 3 /usr/bin/nc -z 127.0.0.1 $LPORT >/dev/null 2>&1 && tmo 3 /usr/bin/nc -z 127.0.0.1 $PPORT >/dev/null 2>&1 && break
  sleep 0.5; i=$((i+1))
done
APID=$(/usr/bin/pgrep -u $ALICE -f CANARY-ARG-SECRET | head -1)
[ -n "$APID" ] || { say "SETUP FAILED: canary process did not start"; exit 1; }

# sudo rule, first version: only the rule, Apple's default sudo settings (no use_pty, default env_keep).
# Both versions turn off sudo's "incident" mails for the two test users, so the refused-sudo tests
# do not put mail into root's mailbox (the tests do not depend on mail).
write_sudoers() { # full|base
  {
    echo "Defaults:$ALICE !mail_badpass, !mail_no_host, !mail_no_perms, !mail_no_user, !mail_always"
    echo "Defaults:$BOX !mail_badpass, !mail_no_host, !mail_no_perms, !mail_no_user, !mail_always"
    if [ "$1" = full ]; then
      echo "Defaults>$BOX use_pty, env_reset"
      echo "Defaults>$BOX env_keep = \"TERM LANG LC_ALL COLORTERM\""
    fi
    echo "$ALICE ALL=($BOX) NOPASSWD: $KIT/libexec/box-start"
  } >$KIT/tmp/sudoers.new
  tmo 20 visudo -cf $KIT/tmp/sudoers.new >>"$LOG" 2>&1 || return 1
  [ -L "$SUDOERS" ] && return 1
  install -m 0440 -o root -g wheel $KIT/tmp/sudoers.new $SUDOERS || { rm -f "$SUDOERS"; return 1; }
  # the whole sudo setup must still be valid with our file in it; if not, take it out at once
  # (output not kept: it lists the Mac's other sudo files)
  tmo 20 visudo -c >/dev/null 2>&1 || { rm -f "$SUDOERS"; say "  sudo rejected the kit's rule file; removed it again"; return 1; }
}
write_sudoers base || { say "SETUP FAILED: sudoers (see $LOG)"; exit 1; }
must "sudo rule works" /usr/bin/sudo -n -u $ALICE -H /usr/bin/sudo -n -u $BOX $KIT/libexec/box-start /usr/bin/true

# clipboard
# (the saved copy sits in /Library/Kaptest/tmp, root only, mode 600, and is deleted at the end)
if [ "$KAPTEST_CLIPBOARD" = yes ]; then
  ( umask 077; : >$KIT/tmp/clip.saved )
  if tmo 10 /usr/bin/sudo -n -u "$SUDO_USER" /usr/bin/pbpaste >$KIT/tmp/clip.saved 2>/dev/null; then
    CLIP=1
    printf '%s' "$CLIPCANARY" | tmo 10 /usr/bin/sudo -n -u "$SUDO_USER" /usr/bin/pbcopy 2>>"$LOG"
  else
    rm -f $KIT/tmp/clip.saved; KAPTEST_CLIPBOARD="no (could not read it, so it was not touched)"
  fi
fi
if [ "${KAPTEST_NO_INTERNET:-}" = 1 ]; then NETOK=0
elif tmo 8 curl -sS -m5 -o /dev/null https://example.com >/dev/null 2>&1; then NETOK=1; fi

# facts about this Mac (nothing personal)
rec META-OS "sw_vers; uname -m" "recorded" "$(sw_vers -productVersion) build $(sw_vers -buildVersion) $(uname -m)" INFO
rec META-SUDO "sudo -V | head -1" "recorded" "$(tmo 10 sudo -V 2>&1 | head -1)" INFO
rec META-PF "pfctl -s info | head -1" "recorded" "$(tmo 10 pfctl -s info 2>&1 | grep -i status | head -1)" INFO
rec META-SIP "csrutil status" "recorded" "$(tmo 10 csrutil status 2>&1 | head -1)" INFO
rec META-NETWORK "curl example.com from root" "recorded" "reachable=$NETOK skipped-by-choice=${KAPTEST_NO_INTERNET:-0}" INFO
rec META-CLIPBOARD "clipboard test allowed" "recorded" "$KAPTEST_CLIPBOARD" INFO
rec META-SANDBOX-EXEC "command -v sandbox-exec" "recorded" "present=$HAVE_SB" INFO

# ---------------------------------------------------------------- the attack suite

net_tests() { # phase
  local P=$1 e1 e2 e3 e4 e5 e6
  case $P in
    A|B) e1=leak;  e2=leak;  e3=leak;  e4=leak;  e5=leak;  e6=leak ;;
    C)   e1=block; e2=block; e3=block; e4=leak;  e5=block; e6=block ;;
    D)   e1=block; e2=leak;  e3=info;  e4=info;  e5=leak;  e6=block ;;
  esac
  if [ $NETOK = 1 ]; then
    tc "$P-NET-OTHERHOST"   $e1 NET-REACHED run_abox 1 "/usr/bin/curl -sS -m5 -o /dev/null https://example.com && echo NET-REACHED"
    tc "$P-NET-ALLOWEDIP"   $e2 NET-REACHED run_abox 1 "/usr/bin/curl -sS -m5 -k -o /dev/null https://1.1.1.1 && echo NET-REACHED"
    tc "$P-NET-PING"        $e5 NET-REACHED run_abox 1 "/sbin/ping -c1 -t3 1.1.1.1 >/dev/null && echo NET-REACHED"
    tc "$P-NET-UDP-DNS"     $e6 NET-REACHED run_abox 1 "/usr/bin/dig +time=2 +tries=1 @1.1.1.1 example.com +short | grep -Eq '^[0-9]+[.]' && echo NET-REACHED"
  else
    for n in OTHERHOST ALLOWEDIP PING UDP-DNS; do rec "$P-NET-$n" "(needs internet)" "-" "no internet from this Mac" SKIP; done
  fi
  tc "$P-NET-LOCALHOST-SERVICE" $e3 CANARY-LOCALHOST-3c9d run_abox 1 "/usr/bin/curl -sS -m3 http://127.0.0.1:$LPORT"
  tc "$P-NET-LOCALHOST-ALLOWEDPORT" $e4 PROXY-OK run_abox 1 "/usr/bin/curl -sS -m3 http://127.0.0.1:$PPORT"
}

suite() { # phase A (default modes, no walls) | B (home 700 + sudo rule) | C (+ Seatbelt)
  local P=$1 HX SX fid fpath fcan fbase ex
  case $P in A) HX=leak;; *) HX=block;; esac
  case $P in C) SX=block;; *) SX=leak;; esac

  # files and listings
  while IFS='|' read -r fid fpath fcan fbase; do
    ex=$HX; [ "$fbase" = block ] && ex=block
    tc "$P-FILE-$fid" $ex "$fcan" run_abox 1 "/bin/cat $fpath"
  done <<EOF
PERSONAL|$AHOME/workspace/private-note.txt|CANARY-PERSONAL-FILE-7f3a|leak
GITCONFIG|$AHOME/.gitconfig|CANARY-GITCONFIG-1b6c|leak
CHAT|$AHOME/.claude/projects/-demo/chat.jsonl|CANARY-PERSONAL-CHAT-2e8f|leak
KEYCHAIN|$AHOME/Library/Keychains/login.keychain-db|CANARY-KEYCHAIN-d00d|leak
SSHKEY|$AHOME/.ssh/id_fake|CANARY-SSH-KEY-c0de|block
CLAUDECRED|$AHOME/.claude/.credentials.json|CANARY-CLAUDE-CRED-b41d|block
DOCS|$AHOME/Documents/secret.txt|CANARY-DOCS-4a90|block
EOF
  tc "$P-LIST-PERSON-HOME"  $HX CANARY-DIR-LISTING-8d3e run_abox 1 "/bin/ls -A $AHOME"
  tc "$P-LIST-USERS"        $SX kaptest-alice run_abox 1 "/bin/ls /Users 2>&1 | grep kaptest-"
  tc "$P-FIND-HOME"         $HX private-note.txt run_abox 1 "/usr/bin/find $AHOME -name 'private-note*' 2>&1"

  # shared places outside the home
  tc "$P-TMP-FILE"          $SX CANARY-TMP-5e0d     run_abox 1 "/bin/cat $TMPCANARY"
  tc "$P-VARTMP-FILE"       $SX CANARY-VTMP-9a11    run_abox 1 "/bin/cat $VTMPCANARY"
  tc "$P-USERS-SHARED-FILE" $SX CANARY-SHARED-77f1  run_abox 1 "/bin/cat $OUTSIDE"
  tc "$P-TMP-WRITE"         $SX WRITE-OK            run_abox 1 "echo x >/private/tmp/kaptest-box-w && echo WRITE-OK"
  tc "$P-CONTROL-BOX-OWN-FILE" leak BOX-OWN-FILE-1a2b run_abox 0 "/bin/cat $BHOME/own.txt"

  # sockets and the ssh-agent
  tc "$P-SOCK-HOME"   $HX CANARY-SOCKET-HOME-6b02 run_abox 1 "/usr/bin/perl -MIO::Socket::UNIX -e 'my \$s=IO::Socket::UNIX->new(Peer=>\$ARGV[0]) or die \"connect: \$!\\n\"; print scalar <\$s>' $AHOME/.sockdir/s.sock"
  tc "$P-SOCK-TMP"    $SX CANARY-SOCKET-TMP-91ce  run_abox 1 "/usr/bin/perl -MIO::Socket::UNIX -e 'my \$s=IO::Socket::UNIX->new(Peer=>\$ARGV[0]) or die \"connect: \$!\\n\"; print scalar <\$s>' $TMPSOCK"
  tc "$P-SSH-AGENT"  $HX CANARY-AGENTKEY-77aa    run_abox 1 "SSH_AUTH_SOCK=$AHOME/.sockdir/agent.sock /usr/bin/ssh-add -l"

  # the person's processes
  tc "$P-PROC-NAME-VISIBLE" $( [ $P = C ] && echo block || echo leak ) sh run_abox 0 "/bin/ps -p $APID -o comm= 2>/dev/null"
  tc "$P-PROC-ARGS-ps"      block CANARY-ARG-SECRET-4d1e run_abox 0 "/bin/ps -ww -p $APID -o args="
  tc "$P-PROC-ENV-ps"       block CANARY-ENV-SECRET-5a77 run_abox 0 "/bin/ps -E -ww -p $APID 2>&1"
  tc "$P-PROC-ARGS-sysctl"  block CANARY-ARG-SECRET-4d1e run_abox 0 "/usr/sbin/sysctl -b kern.procargs2.$APID 2>&1 | tr -c '[:print:]' ' '"
  tc "$P-PROC-ENV-sysctl"   block CANARY-ENV-SECRET-5a77 run_abox 0 "/usr/sbin/sysctl -b kern.procargs2.$APID 2>&1 | tr -c '[:print:]' ' '"
  tc "$P-PROC-LSOF"         block "$AHOME" run_abox 0 "/usr/sbin/lsof -p $APID 2>&1"
  tc "$P-PROC-SIGNAL"       block KILL-OK run_abox 1 "/bin/kill -0 $APID && echo KILL-OK"

  # network
  net_tests $P

  # clipboard
  if [ $CLIP = 1 ]; then
    REDACT=1
    tc "$P-CLIPBOARD" $( [ $P = C ] && echo block || echo leak ) "$CLIPCANARY" run_abox 0 "/usr/bin/pbpaste 2>&1"
    REDACT=0
  else
    rec "$P-CLIPBOARD" "pbpaste" "-" "clipboard test not allowed" SKIP
  fi
}

say ""; say "== Phase A: before any wall (macOS defaults; the box CAN see things, this proves the tests work) =="
suite A
tiocsti() { # id expect
  local id=$1 ex=$2 out res expd
  out=$( { sleep 7; } | tmo 45 /usr/bin/sudo -n -u $ALICE -H /usr/bin/script -q /dev/null /bin/sh -c \
     "/usr/bin/sudo -n -u $BOX $KIT/libexec/box-start /usr/bin/perl $KIT/sti.pl; sleep 1; IFS= read -r -t 3 line; echo \"GOT:[\$line]\"" 2>&1 | tr -d '\r')
  case $out in
    *"GOT:["*) if [ $ex = leak ]; then expd="typed text reaches the person's terminal"; case $out in *"GOT:[echo INJECTED"*) res=PASS;; *) res=FAIL;; esac
               else expd="nothing reaches the person's terminal"; case $out in *"GOT:[echo INJECTED"*) res=FAIL;; *) res=PASS;; esac; fi ;;
    *) expd="test terminal ran"; res=SKIP; out="the pseudo-terminal helper (script) did not run: $out" ;;
  esac
  rec "$id" "script -q /dev/null: sudo -u box box-start perl sti.pl; read keystrokes" "$expd" "$out" "$res"
}
tiocsti A-TIOCSTI leak
for v in "SSH_AUTH_SOCK=/tmp/kaptest-fake-agent" "KAPTEST_ENVLEAK=CANARY-ENVLEAK-1a2b" "HOME=$AHOME"; do
  tc "A-ENV-$(printf '%s' "${v%%=*}")" leak "$v" run_abox_env 0 "/usr/bin/env | grep -E 'SSH_AUTH_SOCK|ENVLEAK|^HOME='"
done

say ""; say "== Phase B: Unix walls (home 700, sudo rule with use_pty and env_reset) =="
chmod 700 $AHOME
rec B-HOME-MODE "stat -f %Lp $AHOME" "700" "$(stat -f %Lp $AHOME)" "$( [ "$(stat -f %Lp $AHOME)" = 700 ] && echo PASS || echo FAIL )"
write_sudoers full && rec B-SUDOERS-VALID "visudo -cf (use_pty, env_reset, env_keep) then install" "accepted by visudo" "accepted" PASS || rec B-SUDOERS-VALID "visudo -cf" "accepted by visudo" "visudo rejected the file (see log)" FAIL
suite B
tiocsti B-TIOCSTI block
for v in "SSH_AUTH_SOCK=/tmp/kaptest-fake-agent" "KAPTEST_ENVLEAK=CANARY-ENVLEAK-1a2b" "HOME=$AHOME"; do
  tc "B-ENV-$(printf '%s' "${v%%=*}")" block "$v" run_abox_env 0 "/usr/bin/env | grep -E 'SSH_AUTH_SOCK|ENVLEAK|^HOME='"
done
# the box account itself
tc B-BOX-HIDDEN       leak "IsHidden: 1" run_root 0 "dscl . -read /Users/$BOX IsHidden"
tc B-BOX-NO-LOGIN     leak "No such key" run_root 0 "dscl . -read /Users/$BOX AuthenticationAuthority 2>&1"
tc B-BOX-NOT-ADMIN    block admin run_root 0 "id -Gn $BOX"
tc B-BOX-NOT-STAFF    block staff run_root 0 "id -Gn $BOX"
tc B-BOX-NO-SUDO      block GOT-ROOT run_abox 1 "/usr/bin/sudo -n /usr/bin/true && echo GOT-ROOT"
tc B-RULE-IS-NARROW   block SHELL-AS-BOX run_alice 1 "/usr/bin/sudo -n -u $BOX /bin/sh -c 'echo SHELL-AS-BOX'"
tc B-PERSON-READS-BOX-HOME block BOX-OWN-FILE-1a2b run_alice 1 "/bin/cat $BHOME/own.txt"
if [ -n "$BOXTMP" ]; then
  tc B-PERSON-READS-BOX-TMP block CANARY-BOXTMP-3f4e run_alice 1 "/bin/cat $BOXTMP/box-canary"
  rec B-BOX-TMP-MODE "stat -f %Lp \$TMPDIR of the box" "700" "$(stat -f %Lp "$BOXTMP")" "$( [ "$(stat -f %Lp "$BOXTMP")" = 700 ] && echo PASS || echo FAIL )"
fi
tc B-SPOTLIGHT-WEAK info CANARY-DIR-LISTING-8d3e run_abox 0 "/usr/bin/mdfind -name CANARY-DIR-LISTING-8d3e 2>&1 | head -3  # weak: a new file may not be indexed yet"

say ""; say "== Phase B2: team area =="
mkdir $TEAMROOT && : >$TEAMROOT/$DIRMARK && chown root:wheel $TEAMROOT && chmod 755 $TEAMROOT
mkdir -p $TEAMROOT/alice && chown $BUID:$SGID $TEAMROOT/alice && chmod 2770 $TEAMROOT/alice
tc T-MODE-ROOT   leak 755  run_root 0 "stat -f %Lp $TEAMROOT"
tc T-MODE-PERSON leak 2770 run_root 0 "stat -f %Lp $TEAMROOT/alice"
tc T-PERSON-WRITES leak PERSON-WROTE run_alice 0 "echo PERSON-WROTE >$TEAMROOT/alice/p.txt && /bin/cat $TEAMROOT/alice/p.txt"
tc T-BOX-READS-PERSON leak PERSON-WROTE run_abox 0 "/bin/cat $TEAMROOT/alice/p.txt"
tc T-BOX-WRITES-GROUP leak $SHARE run_abox 0 "echo BOX-WROTE >$TEAMROOT/alice/b.txt && stat -f %Sg $TEAMROOT/alice/b.txt"
tc T-PERSON-READS-BOX-FILE leak BOX-WROTE run_alice 0 "/bin/cat $TEAMROOT/alice/b.txt"
tc T-OUTSIDER-LIST block PERSON-WROTE run_nobody 0 "/bin/ls /Users/Shared/kaptest-team/alice; /bin/cat $TEAMROOT/alice/p.txt"
tc T-OUTSIDER-WRITE block OUTSIDER-WROTE run_nobody 1 "echo OUTSIDER-WROTE >$TEAMROOT/alice/o.txt && echo OUTSIDER-WROTE"

say ""; say "== Phase C: Seatbelt profile around the box =="
RELAYSOCK=$KIT/relay.sock
BOXVAR=""; [ -n "$BOXTMP" ] && BOXVAR=$(dirname "$BOXTMP")
SBVAR=""; [ -n "$BOXVAR" ] && SBVAR="(subpath \"$BOXVAR\")"   # never an empty (subpath "")
write_strict() {
cat >$KIT/box.sb <<EOF
(version 1)
(deny default)
(import "system.sb")
(allow process-exec*)
(allow process-fork)
(allow sysctl-read)
(allow file-read-metadata)
(allow file-read* (subpath "/usr") (subpath "/bin") (subpath "/sbin") (subpath "/System") (subpath "/Library") (subpath "/Applications") (subpath "/private/etc") (subpath "/private/var/db") (subpath "/dev") (subpath "/opt"))
(allow file-read* file-write* (subpath "$BHOME") (subpath "$TEAMROOT/alice") $SBVAR)
(allow file-write* (literal "/dev/null") (literal "/dev/dtracehelper") (regex #"^/dev/tty") (regex #"^/dev/ttys[0-9]+\$"))
(allow pseudo-tty)
(allow network-outbound (remote ip "localhost:$PPORT"))
(allow network-outbound (remote unix-socket (path-literal "$RELAYSOCK")))
(allow process-info* (target same-sandbox))
(allow signal (target same-sandbox))
(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo") (global-name "com.apple.logd") (global-name "com.apple.system.notification_center") (global-name "com.apple.trustd") (global-name "com.apple.SecurityServer") (global-name "com.apple.system.logger") (global-name "com.apple.cfprefsd.daemon") (global-name "com.apple.bsd.dirhelper"))
(deny mach-lookup (global-name "com.apple.pasteboard.1") (global-name "com.apple.CARenderServer") (global-name "com.apple.windowserver.active") (global-name "com.apple.coreservices.appleevents"))
EOF
}
write_lenient() {
cat >$KIT/box.sb <<EOF
(version 1)
(allow default)
(deny file-write*)
(allow file-write* (subpath "$BHOME") (subpath "$TEAMROOT/alice") $SBVAR (literal "/dev/null") (literal "/dev/dtracehelper") (regex #"^/dev/tty") (regex #"^/dev/ttys[0-9]+\$"))
(deny file-read* (subpath "/Users") (subpath "/private/tmp") (subpath "/private/var/tmp"))
(allow file-read* (subpath "$BHOME") (subpath "$TEAMROOT/alice"))
(allow file-read-metadata (literal "/Users") (literal "/Users/Shared") (literal "$BHOME"))
(deny network*)
(allow network-outbound (remote ip "localhost:$PPORT"))
(allow network-outbound (remote unix-socket (path-literal "$RELAYSOCK")))
(deny process-info*)
(allow process-info* (target same-sandbox))
(deny signal)
(allow signal (target same-sandbox))
(deny mach-lookup (global-name "com.apple.pasteboard.1") (global-name "com.apple.CARenderServer") (global-name "com.apple.windowserver.active") (global-name "com.apple.coreservices.appleevents"))
EOF
}
probe_profile() {
  local o
  o=$(run_abox "/bin/echo SB-OK; /bin/cat $BHOME/own.txt; echo x >$TEAMROOT/alice/sb.txt && echo TEAM-OK" 2>&1)
  case $o in *SB-OK*BOX-OWN-FILE-1a2b*TEAM-OK*) return 0;; esac
  PROBE_OUT=$o; return 1
}
PROBE_OUT=""
if [ $HAVE_SB = 1 ]; then
  set_mode seatbelt
  write_strict
  if probe_profile; then
    PROFILE=strict
    rec C-PROFILE-STARTS "strict profile: (deny default) + system.sb, probe: echo, read own file, write team area" "tool starts under the profile" "works" PASS
  else
    rec C-PROFILE-STARTS "strict profile: (deny default) + system.sb, probe" "tool starts under the profile" "FAILED to start: $PROBE_OUT" FAIL
    write_lenient
    if probe_profile; then
      PROFILE=lenient
      rec C-PROFILE-LENIENT "fallback profile: (allow default) + denies, same probe" "tool starts under the profile" "works" PASS
    else
      rec C-PROFILE-LENIENT "fallback profile, same probe" "tool starts under the profile" "FAILED to start: $PROBE_OUT" FAIL
    fi
  fi
else
  rec C-PROFILE-STARTS "sandbox-exec" "present" "not installed on this Mac" SKIP
fi
rec META-PROFILE "which profile the C tests used" "recorded" "$PROFILE" INFO
if [ "$PROFILE" != none ]; then
  suite C
  tc C-TEAM-WRITE-CONTROL leak TEAM-WRITE-OK run_abox 0 "echo x >$TEAMROOT/alice/c.txt && echo TEAM-WRITE-OK"
  tc C-WRITE-OUTSIDE-TEAM block WRITE-OK run_abox 1 "echo x >/Users/Shared/kaptest-box-w && echo WRITE-OK"
  tc C-TOOL-STARTS leak uid= run_abox 0 "/usr/bin/id"
  # does a tool's own sandbox nest? (cannot be checked without the tool; see README)
  tc C-NESTED-SANDBOX info NESTED-OK run_abox 0 "/usr/bin/sandbox-exec -p '(version 1)(allow default)' /bin/echo NESTED-OK 2>&1"
else
  rec C-ALL "phase C" "ran" "skipped: no working Seatbelt profile" SKIP
fi
set_mode plain
# put the clipboard back right away (only if our fake text is still on it)
if [ $CLIP = 1 ]; then
  restore_clip
  rm -f $KIT/tmp/clip.saved
  CLIP=0
fi

say ""; say "== Phase D: pf rules for the box user (second network layer; Seatbelt is off here) =="
cat >$KIT/tmp/kaptest.pf <<EOF
pass out quick proto tcp to 1.1.1.1 port 443 user $BOX
block drop out quick proto { tcp udp } all user $BOX
EOF
PFBEFORE=$(pf_status)
case $PFBEFORE in Enabled|Disabled) ;; *) PFBEFORE="";; esac
if [ -z "$PFBEFORE" ]; then
  rec D-PF-ANCHOR-LOADED "pfctl -s info" "pf status readable" "could not read pf status; pf tests skipped so nothing is changed" SKIP
elif tmo 30 pfctl -a $ANCHOR -f $KIT/tmp/kaptest.pf >>"$LOG" 2>&1; then
  # remember the state and the token in files too, so --cleanup can undo it after a hard stop
  printf '%s\n' "$PFBEFORE" >$KIT/pf.before; : >$KIT/pf.used; PFUSED=1
  PFTOKEN=$(tmo 20 pfctl -E 2>&1 | tee -a "$LOG" | awk '/Token/{print $NF; exit}')
  case $PFTOKEN in ''|*[!0-9]*) PFTOKEN="";; esac
  [ -n "$PFTOKEN" ] && printf '%s\n' "$PFTOKEN" >$KIT/pf.token
  sleep 1
  o=$(tmo 20 pfctl -a $ANCHOR -s rules 2>&1 | grep -c "user")
  rec D-PF-ANCHOR-LOADED "pfctl -a $ANCHOR -s rules | grep -c user" "2 rules loaded" "$o rules; token=${PFTOKEN:-none}" "$( [ "$o" = 2 ] && echo PASS || echo FAIL )"
  net_tests D
  o=$(tmo 20 pfctl -a $ANCHOR -s rules 2>&1 | grep -c "user")
  rec D-PF-RULES-STILL-THERE "pfctl -a $ANCHOR -s rules after the tests" "2 rules" "$o rules" "$( [ "$o" = 2 ] && echo PASS || echo FAIL )"
else
  rec D-PF-ANCHOR-LOADED "pfctl -a $ANCHOR -f rules" "rules load" "pfctl refused (see log)" FAIL
fi

say ""; say "== Done testing. Removing everything =="
finish
