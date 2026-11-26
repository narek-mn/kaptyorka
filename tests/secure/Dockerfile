# Throwaway test computer for the secure-kaptyorka red team: systemd as PID 1,
# a person account with fake personal data, nothing from the real laptop.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus python3 python3-venv python3-pip git curl ca-certificates \
      sudo procps iproute2 tmux openssl acl && rm -rf /var/lib/apt/lists/*
RUN useradd -m -s /bin/bash alice && \
    mkdir -p /home/alice/personal /home/alice/.ssh /home/alice/.claude/sessions /home/alice/.claude/projects/-home-alice-personal /home/alice/team/proj-a && \
    echo "CANARY-PERSONAL-FILE-7f3a" > /home/alice/personal/diary.txt && \
    echo "CANARY-SSH-KEY-91bd" > /home/alice/.ssh/id_ed25519 && \
    echo '{"claudeAiOauth":{"accessToken":"CANARY-CLAUDE-TOKEN-c0de"}}' > /home/alice/.claude/.credentials.json && \
    echo '{"type":"user","message":{"content":"CANARY-PERSONAL-CHAT-55aa"}}' > /home/alice/.claude/projects/-home-alice-personal/chat.jsonl && \
    echo "team readme" > /home/alice/team/proj-a/README.md && \
    chown -R alice:alice /home/alice && chmod 700 /home/alice /home/alice/.ssh && chmod 600 /home/alice/.ssh/id_ed25519 /home/alice/.claude/.credentials.json
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
