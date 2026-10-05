#!/usr/bin/env bash
# Stand-in for system commands in demo mode and in tests. It is symlinked as
# "ip", "systemctl", "kubectl"... and answers from rules files instead of
# touching the machine.
#
# NODEYARD_SHIM_RULES  colon-separated rules files, searched in order
# NODEYARD_SHIM_DATA   directory for "@file" outputs
# NODEYARD_SHIM_LOG    optional: every invocation is appended here
#
# Rules file lines (tab-separated):  GLOB<TAB>EXIT_CODE<TAB>OUTPUT
#   GLOB is matched against "name arg1 arg2 ..."; first match wins.
#   OUTPUT is "@relative/file" (printed verbatim), text with \n escapes, or empty.

name="${0##*/}"
line="${name}${*:+ $*}"

if [[ -n "${NODEYARD_SHIM_LOG:-}" ]]; then
    printf '%s\n' "$line" >>"$NODEYARD_SHIM_LOG"
fi

IFS=':' read -r -a rule_files <<<"${NODEYARD_SHIM_RULES:-}"
for rules in "${rule_files[@]}"; do
    [[ -r "$rules" ]] || continue
    while IFS=$'\t' read -r pattern rc out || [[ -n "$pattern" ]]; do
        [[ -z "$pattern" || "$pattern" == \#* ]] && continue
        # shellcheck disable=SC2053 # the pattern is a glob on purpose
        if [[ "$line" == $pattern ]]; then
            if [[ "$out" == @* ]]; then
                cat "${NODEYARD_SHIM_DATA:-.}/${out#@}"
            elif [[ -n "$out" ]]; then
                printf '%b\n' "$out"
            fi
            exit "${rc:-0}"
        fi
    done <"$rules"
done

printf 'shim: no rule for: %s\n' "$line" >&2
exit 127
