# kaptyorka launcher functions (secure design v2, 4.7). Root-owned, fixed. Sourced by bash and zsh
# from the system start files. A function wins over PATH, so ~/.local/bin cannot shadow it.
claude()       { /usr/local/libexec/kaptyorka/launch claude "$@"; }
opencode()     { /usr/local/libexec/kaptyorka/launch opencode "$@"; }
codex()        { /usr/local/libexec/kaptyorka/launch codex "$@"; }
cursor-agent() { /usr/local/libexec/kaptyorka/launch cursor-agent "$@"; }
git()          { /usr/local/libexec/kaptyorka/launch git "$@"; }
# bash hands the four tools' functions to its children, so `bash -c 'opencode'` and bash scripts
# go through the launcher too (git is not handed on: every script's git would pay for it)
if [ -n "${BASH_VERSION:-}" ]; then export -f claude opencode codex cursor-agent 2>/dev/null; fi
