#!/usr/bin/env bash
# Runs inside a fresh distro container (see run.sh): installs nodeyard from
# /src, exercises the core commands, and removes it again.
# Usage: in-container.sh EXPECTED_PACKAGE_MANAGER
set -euo pipefail

expect_pkg="$1"
step() { printf '\n== %s\n' "$*"; }
fail() {
    printf 'HARNESS FAILURE: %s\n' "$*" >&2
    exit 1
}

step "install.sh from the source tree (installs required packages too)"
bash /src/install.sh --from-dir /src --yes
command -v nodeyard >/dev/null || fail "nodeyard is not on PATH after install"
nodeyard --version

step "detect"
nodeyard detect
json="$(nodeyard detect --json)"
pkg="$(jq -r '.package_manager' <<<"$json")"
support="$(jq -r '.os.support' <<<"$json")"
[[ "$pkg" == "$expect_pkg" ]] || fail "package manager: expected ${expect_pkg}, got ${pkg}"
[[ "$support" == supported || "$support" == best-effort ]] || fail "support level: ${support}"
jq -e '.arch == "amd64" or .arch == "arm64"' <<<"$json" >/dev/null || fail "unexpected arch"

step "deps"
nodeyard deps --feature core --json | jq -e '[.dependencies[] | select(.status != "installed")] | length == 0' >/dev/null ||
    fail "core dependencies are missing after install"

step "doctor"
nodeyard doctor --json | jq -e '.ok == true' >/dev/null || fail "doctor --json did not report ok"
nodeyard doctor || true

step "re-running install is a no-op"
out="$(bash /src/install.sh --from-dir /src --yes 2>&1)"
grep -q "already installed" <<<"$out" || fail "second install did something: ${out}"

step "config change, journal and undo"
nodeyard config set network.subnet 192.168.50.0/24 --yes
[[ "$(nodeyard config get network.subnet)" == 192.168.50.0/24 ]] || fail "config get"
nodeyard changes --json | jq -e '.changes | length == 1' >/dev/null || fail "journal does not list the change"
nodeyard undo --last --yes
[[ ! -s /etc/nodeyard/cluster.conf ]] || fail "undo did not remove the config file it created"

step "dry runs change nothing"
# (ls, not find: minimal images such as Tumbleweed's have no findutils)
# shellcheck disable=SC2012 # a listing is all we compare
snapshot() { ls -laR --time-style=full-iso /etc 2>/dev/null | md5sum; }
before="$(snapshot)"
nodeyard install master --ha --dry-run --yes >/dev/null 2>&1 || true
after="$(snapshot)"
[[ "$before" == "$after" ]] || fail "install master --dry-run changed files under /etc"

step "uninstall.sh"
bash /src/uninstall.sh --yes
hash -r
if [[ -e /usr/local/bin/nodeyard ]] || command -v nodeyard >/dev/null 2>&1; then
    fail "nodeyard still on PATH after uninstall"
fi
[[ ! -e /usr/local/lib/nodeyard && ! -e /etc/nodeyard ]] || fail "files left behind after uninstall"

printf '\nHARNESS OK\n'
