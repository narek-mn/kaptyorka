# >>> kaptyorka >>>  (written by kaptyorka-secure-setup; removed by its remove command)
case ":${PATH:-}:" in *:/usr/local/libexec/kaptyorka/shims:*) ;; *) PATH="/usr/local/libexec/kaptyorka/shims${PATH:+:$PATH}"; export PATH ;; esac
if [ -n "${BASH_VERSION:-}" ] && ! shopt -oq posix 2>/dev/null || [ -n "${ZSH_VERSION:-}" ]; then
  . /etc/kaptyorka/launcher.sh
fi
# <<< kaptyorka <<<
