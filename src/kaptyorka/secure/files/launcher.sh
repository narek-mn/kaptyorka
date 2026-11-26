# kaptyorka launcher functions (secure design v2, 4.7). Root-owned, fixed. Sourced by bash and zsh
# from the system start files. A function wins over PATH, so ~/.local/bin cannot shadow it.
claude()       { /usr/local/libexec/kaptyorka/launch claude "$@"; }
opencode()     { /usr/local/libexec/kaptyorka/launch opencode "$@"; }
codex()        { /usr/local/libexec/kaptyorka/launch codex "$@"; }
cursor-agent() { /usr/local/libexec/kaptyorka/launch cursor-agent "$@"; }
git()          { /usr/local/libexec/kaptyorka/launch git "$@"; }
