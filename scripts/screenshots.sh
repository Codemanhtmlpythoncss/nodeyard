#!/usr/bin/env bash
# Regenerate the terminal screenshots in docs/media/ from demo mode.
# Needs bash 4.3+, jq and python3.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd -P)"
media="${root}/docs/media"
mkdir -p "$media"
export NODEYARD_DEMO_DIR
NODEYARD_DEMO_DIR="$(mktemp -d)"
trap 'rm -rf "$NODEYARD_DEMO_DIR"' EXIT
export NODEYARD_COLOR=always COLUMNS=96 LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

ny() { "${root}/bin/nodeyard" --demo "$@" 2>&1 || true; }
shot() { # NAME TITLE  (stdin: the terminal output)
    python3 "${root}/scripts/ansi2svg.py" "$2" >"${media}/$1.svg"
    echo "wrote docs/media/$1.svg"
}

ny version >/dev/null

# The menu: header and choices (plain prompts, answered with q).
printf 'q\n' | NODEYARD_INTERACTIVE=1 NODEYARD_UI=plain ny menu | sed '$d' |
    {
        cat
        printf 'Choice (q = quit) [1]: \n'
    } | shot menu "sudo nodeyard"

ny status | shot status "sudo nodeyard status"
ny doctor | shot doctor "sudo nodeyard doctor"
ny worker-info | shot worker-info "sudo nodeyard worker-info"
ny detect | shot detect "nodeyard detect"

# A wizard: pick a k3s version, read the summary, then cancel.
printf '1\n3\n' | NODEYARD_INTERACTIVE=1 NODEYARD_UI=plain ny wizard k3s-upgrade |
    grep -v '^Cancelled' | shot wizard "sudo nodeyard wizard k3s-upgrade"

ny install master --ha --worker --interface eth0 --dry-run | head -n 40 |
    shot dry-run "sudo nodeyard install master --ha --worker --interface eth0 --dry-run"
