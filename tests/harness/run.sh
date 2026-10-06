#!/usr/bin/env bash
# Multi-distro harness: install and exercise nodeyard in a fresh container of
# each supported distribution.
#
#   tests/harness/run.sh                 every distro for this machine's architecture
#   tests/harness/run.sh debian-12 arch  just these
#
# Needs Docker (or podman via DOCKER=podman). Containers need internet access
# to install packages. Logs go to tests/harness/logs/.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd -P)"
root="$(cd "${here}/../.." && pwd -P)"
docker="${DOCKER:-docker}"
logs="${here}/logs"
mkdir -p "$logs"

case "$(uname -m)" in
    x86_64 | amd64) host_arch=amd64 ;;
    aarch64 | arm64) host_arch=arm64 ;;
    *) host_arch=unknown ;;
esac

declare -a wanted=("$@")
pass=0 failed=0 skipped=0
declare -a summary=()

while read -r name image pkg platforms; do
    [[ -z "$name" || "$name" == \#* ]] && continue
    if [[ "${#wanted[@]}" -gt 0 ]]; then
        hit=0
        for w in "${wanted[@]}"; do [[ "$w" == "$name" ]] && hit=1; done
        [[ "$hit" -eq 1 ]] || continue
    fi
    if [[ ",${platforms}," != *",${host_arch},"* ]]; then
        summary+=("SKIP  ${name} (no ${host_arch} image)")
        skipped=$((skipped + 1))
        continue
    fi
    printf '>> %-22s %s\n' "$name" "$image"
    # Make the container look like an installed system: Arch images ship
    # without a synced package database (a real machine has one, and
    # nodeyard never runs a partial 'pacman -Sy'). pacman's download
    # sandbox can't start inside some container runtimes, so it is turned
    # off in the container only.
    bootstrap='if command -v pacman >/dev/null; then sed -i "/^\[options\]/a DisableSandbox" /etc/pacman.conf; pacman -Syu --noconfirm >/dev/null 2>&1 || true; fi'
    if "$docker" run --rm -v "${root}:/src:ro" "$image" \
        sh -c "${bootstrap}; bash /src/tests/harness/in-container.sh ${pkg}" >"${logs}/${name}.log" 2>&1; then
        summary+=("PASS  ${name}")
        pass=$((pass + 1))
    else
        summary+=("FAIL  ${name}  (see tests/harness/logs/${name}.log)")
        failed=$((failed + 1))
        tail -n 15 "${logs}/${name}.log" | sed 's/^/     /'
    fi
done <"${here}/distros.txt"

printf '\n'
printf '%s\n' "${summary[@]}"
printf '\n%d passed, %d failed, %d skipped\n' "$pass" "$failed" "$skipped"
[[ "$failed" -eq 0 ]]
