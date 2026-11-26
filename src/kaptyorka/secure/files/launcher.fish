# kaptyorka launcher functions for fish (secure design v2, 4.7). Root-owned, fixed.
contains /usr/local/libexec/kaptyorka/shims $PATH; or set -gx PATH /usr/local/libexec/kaptyorka/shims $PATH
function claude;       /usr/local/libexec/kaptyorka/launch claude $argv; end
function opencode;     /usr/local/libexec/kaptyorka/launch opencode $argv; end
function codex;        /usr/local/libexec/kaptyorka/launch codex $argv; end
function cursor-agent; /usr/local/libexec/kaptyorka/launch cursor-agent $argv; end
function git;          /usr/local/libexec/kaptyorka/launch git $argv; end
