# bash completion for nodeyard (and its k3s-manager alias).
# nodeyard answers the candidates itself, so this never goes out of date.
_nodeyard() {
    local cur="${COMP_WORDS[COMP_CWORD]}"
    local -a words=("${COMP_WORDS[@]:1:COMP_CWORD-1}")
    local IFS=$'\n'
    # shellcheck disable=SC2207
    COMPREPLY=($("${COMP_WORDS[0]}" __complete "${words[@]}" -- "$cur" 2>/dev/null))
}
complete -F _nodeyard nodeyard k3s-manager
