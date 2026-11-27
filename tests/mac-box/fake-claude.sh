#!/bin/bash
# A fake `claude` for the macOS box test (tests/mac-box/run.sh). It is installed into the box's
# tools folder (--tools-from) and so runs exactly where a real claude would: as the box user,
# inside the Seatbelt profile, after wallcheck. Its last argument picks what it does:
#   hello     who and where it is, and whether the Claude token arrived on its descriptor
#   attacks   every attack of the wall kit, from inside the box; one RESULT line each
#   tiocsti   tries to type "echo INJECTED" into its terminal (it must land in its own pty)
#   escapes   prints terminal escapes the filter must drop (clipboard, title)
#   write     writes a file in the team folder, appends to the person's file there
# Bash 3.2 (the macOS bash).
mode=${!#}
P=$(/usr/bin/id -un); P=${P#kap-}   # the box of person P is the user kap-P
case $mode in
  hello)
    echo "FAKE-CLAUDE-HELLO uid=$(/usr/bin/id -un) pwd=$(pwd -P)"
    fd=${CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR:-}
    if [ -n "$fd" ]; then
      t=""; IFS= read -r t <&"$fd"
      case $t in sk-ant-oat01-FAKECI*) echo "TOKEN-OK";; *) echo "TOKEN-BAD";; esac
    else
      echo "TOKEN-NONE"
    fi
    ;;
  write)
    echo "written by the box" > ./ci-box-written.txt && echo WRITE-OK
    echo "box line" >> ./person-made.txt && echo APPEND-OK
    echo "box line too" >> ./new-by-person.txt && echo NEW-APPEND-OK
    ;;
  tiocsti)
    /usr/bin/perl -e 'for my $c (split //, "echo INJECTED\n") { ioctl(STDIN, 0x80017472, $c) or do { print "TIOCSTI-FAIL: $!\n"; exit 1 } } print "TIOCSTI-DONE\n";'
    ;;
  escapes)
    printf 'ESC-START\033]52;c;S0FQQ0ktQ0xJUA==\a\033]0;KAPCI-TITLE\a\033]1337;File=x\aESC-END\n'
    ;;
  attacks)
    # $1..: the person's canaries (run.sh passes nothing: the names are fixed here and there)
    H=/Users/$P
    res() { # id expect(block|allow) canary-or-OK output...
      local id=$1 ex=$2 can=$3; shift 3
      local out="$*" got=0
      case $out in *"$can"*) got=1;; esac
      if [ "$ex" = block ]; then
        if [ $got = 1 ]; then echo "RESULT $id FAIL leaked: $(printf '%s' "$out" | tr '\n\t' '  ' | cut -c1-160)"
        else echo "RESULT $id PASS $(printf '%s' "$out" | tr '\n\t' '  ' | cut -c1-120)"; fi
      else
        if [ $got = 1 ]; then echo "RESULT $id PASS"; else echo "RESULT $id FAIL not reached: $(printf '%s' "$out" | tr '\n\t' '  ' | cut -c1-160)"; fi
      fi
    }
    for f in "workspace/private-note.txt:KAPCI-PERSONAL-FILE-7f3a" ".gitconfig:KAPCI-GITCONFIG-1b6c" \
             ".ssh/id_fake:KAPCI-SSH-KEY-c0de" ".claude/.credentials.json:KAPCI-CLAUDE-CRED-b41d" \
             "Library/Keychains/fake.keychain-db:KAPCI-KEYCHAIN-d00d" ".claude/projects/-demo/chat.jsonl:KAPCI-CHAT-2e8f"; do
      res "FILE-${f%%:*}" block "${f#*:}" "$(/bin/cat "$H/${f%%:*}" 2>&1)"
    done
    res FILE-TMP block KAPCI-TMP-5e0d "$(/bin/cat /private/tmp/kapci-canary 2>&1)"
    res FILE-VARTMP block KAPCI-VTMP-9a11 "$(/bin/cat /private/var/tmp/kapci-canary 2>&1)"
    res FILE-USERS-SHARED block KAPCI-SHARED-77f1 "$(/bin/cat /Users/Shared/kapci-canary 2>&1)"
    res FILE-DEVICE-TOKEN block FAKECI-DEVICE "$(/bin/cat /etc/kaptyorka/secret/device-token 2>&1)"
    res FILE-CLAUDE-TOKEN block sk-ant-oat01 "$(/bin/cat /etc/kaptyorka/secret/$P/claude-oauth-token 2>&1)"
    res FILE-SUDOERS block NOPASSWD "$(/bin/cat /etc/sudoers.d/kaptyorka-$P 2>&1)"
    res LIST-HOME block KAPCI-DIR-LISTING "$(/bin/ls -A "$H" 2>&1)"
    res LIST-USERS block "$P" "$(/bin/ls /Users 2>&1)"
    res LIST-TMP block kapci-canary "$(/bin/ls /private/tmp 2>&1)"
    res FIND-HOME block private-note "$(/usr/bin/find "$H" -name 'private-note*' 2>&1)"
    apid=$(cat ./.kapci-person-pid 2>/dev/null)
    res PROC-ARGS-ps block KAPCI-ARG-SECRET "$(/bin/ps -ww -axo args 2>&1)"
    res PROC-ENV-ps block KAPCI-ENV-SECRET "$(/bin/ps -E -ww -ax 2>&1)"
    res PROC-SIGNAL block KILL-OK "$(/bin/kill -0 "${apid:-1}" 2>&1 && echo KILL-OK)"
    res PROC-LSOF block "$H" "$(/usr/sbin/lsof -p "${apid:-1}" 2>&1)"
    res NET-DIRECT block NET-REACHED "$(/usr/bin/curl -sS -m5 --noproxy '*' -o /dev/null https://example.com 2>&1 && echo NET-REACHED)"
    res NET-DIRECT-IP block NET-REACHED "$(/usr/bin/curl -sS -m5 -k --noproxy '*' -o /dev/null https://1.1.1.1 2>&1 && echo NET-REACHED)"
    res NET-PING block NET-REACHED "$(/sbin/ping -c1 -t3 1.1.1.1 >/dev/null 2>&1 && echo NET-REACHED)"
    res NET-UDP-DNS block NET-REACHED "$(/usr/bin/dig +time=2 +tries=1 @1.1.1.1 example.com +short 2>&1 | grep -Eq '^[0-9]+[.]' && echo NET-REACHED)"
    res NET-PERSON-SERVICE block KAPCI-LOCALHOST-3c9d "$(/usr/bin/curl -sS -m3 --noproxy '*' http://127.0.0.1:38917 2>&1)"
    res NET-CANARY-LO block KAPTYORKA-WALL-CANARY "$(/usr/bin/curl -sS -m3 --noproxy '*' http://127.0.0.1:47991 2>&1)"
    res NET-PROXY-REFUSES block NET-REACHED "$(/usr/bin/curl -sS -m10 -o /dev/null https://example.com 2>&1 && echo NET-REACHED)"
    res NET-PROXY-ALLOWED allow PROXY-OK "$(/usr/bin/curl -sS -m15 -o /dev/null -w '%{http_code}' https://api.anthropic.com/ 2>&1 | grep -Eq '^[1-5][0-9][0-9]$' && echo PROXY-OK)"
    res SOCK-TMP block KAPCI-SOCKET-TMP "$(/usr/bin/perl -MIO::Socket::UNIX -e 'my $s=IO::Socket::UNIX->new(Peer=>$ARGV[0]) or die "connect: $!\n"; print scalar <$s>' /private/tmp/kapci.sock 2>&1)"
    res SSH-AGENT block KAPCI-AGENTKEY "$(SSH_AUTH_SOCK=$H/.sockdir/agent.sock /usr/bin/ssh-add -l 2>&1)"
    res CLIPBOARD block KAPCI-CLIP-5f12 "$(/usr/bin/pbpaste 2>&1)"
    res WRITE-TMP block WRITE-OK "$(echo x > /private/tmp/kapci-box-w 2>&1 && echo WRITE-OK)"
    res WRITE-SHARED block WRITE-OK "$(echo x > /Users/Shared/kapci-box-w 2>&1 && echo WRITE-OK)"
    res WRITE-TOOLS block WRITE-OK "$(echo x > /opt/kap-tools/kapci-box-w 2>&1 && echo WRITE-OK)"
    res WRITE-LOCKED-SETTINGS block WRITE-OK "$(echo '{}' > "$HOME/state/claude/settings.json" 2>&1 && echo WRITE-OK)"
    res WRITE-HOME-TOP block WRITE-OK "$(echo x > "$HOME/.zshrc" 2>&1 && echo WRITE-OK)"
    res WRITE-TEAM allow WRITE-OK "$(echo x > ./kapci-team-w 2>&1 && echo WRITE-OK)"
    res ENV-SSH-AUTH-SOCK block SSH_AUTH_SOCK "$(/usr/bin/env 2>&1)"
    res ENV-PERSON block KAPCI-ENVLEAK "$(/usr/bin/env 2>&1)"
    res ENV-HOME block "HOME=$H" "$(/usr/bin/env 2>&1)"
    res NESTED-SANDBOX block NESTED-OK "$(/usr/bin/sandbox-exec -p '(version 1)(allow default)' /bin/echo NESTED-OK 2>&1)"
    res SUDO block GOT-ROOT "$(/usr/bin/sudo -n /usr/bin/true 2>&1 && echo GOT-ROOT)"
    res LIST-TEAMAREA block "$P" "$(/bin/ls /Users/Shared/kaptyorka 2>&1)"
    echo "ATTACKS-DONE"
    ;;
  *)
    echo "fake claude: $mode"
    ;;
esac
