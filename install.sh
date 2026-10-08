#!/usr/bin/env bash
# Install or update nodeyard on this Linux machine.
#
#   curl -fsSL https://raw.githubusercontent.com/Codemanhtmlpythoncss/nodeyard/main/install.sh | sudo bash
#
# or, to read it first:
#   curl -fsSLO https://raw.githubusercontent.com/Codemanhtmlpythoncss/nodeyard/main/install.sh
#   less install.sh && sudo bash install.sh
#
# It downloads the current source from GitHub over HTTPS, installs it under
# /usr/local/lib/nodeyard and links /usr/local/bin/nodeyard. Running it again
# updates in place; use --force to reinstall the same version.

set -euo pipefail

REPO="${NODEYARD_REPO:-Codemanhtmlpythoncss/nodeyard}"
PREFIX="/usr/local"
VERSION=""
FROM_DIR=""
YES=0
DRY_RUN=0
FORCE=0
NO_DEPS=0
SOURCE_COMMIT="${NODEYARD_SOURCE_COMMIT:-}"
REF="main"

usage() {
    cat <<'USAGE'
Usage: install.sh [options]

Options:
  --version X.Y.Z   Install the source at tag vX.Y.Z (default: main)
  --from-dir DIR    Install from an unpacked source archive or git checkout
  --prefix DIR      Install under DIR instead of /usr/local
  --no-deps         Don't offer to install required packages (jq, curl...)
  --force           Reinstall even if this version is already installed
  --dry-run         Show what would happen without changing anything
  --yes, -y         Don't ask for confirmation
  --help, -h        Show this help
USAGE
}

say() { printf '%s\n' "$*" >&2; }
step() { printf '> %s\n' "$*" >&2; }
die() {
    printf 'Error: %s\n' "$1" >&2
    [[ -n "${2:-}" ]] && printf '  Fix: %s\n' "$2" >&2
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)
            [[ $# -ge 2 ]] || die "--version needs a value"
            VERSION="${2#v}"
            [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]] || die "--version must look like 1.2.3"
            REF="tags/v${VERSION}"
            shift 2
            ;;
        --from-dir)
            [[ $# -ge 2 ]] || die "--from-dir needs a directory"
            FROM_DIR="$2"
            shift 2
            ;;
        --prefix)
            [[ $# -ge 2 ]] || die "--prefix needs a directory"
            PREFIX="${2%/}"
            shift 2
            ;;
        --no-deps)
            NO_DEPS=1
            shift
            ;;
        --force)
            FORCE=1
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --yes | -y)
            YES=1
            shift
            ;;
        --help | -h)
            usage
            exit 0
            ;;
        *)
            usage >&2
            die "Unknown option: $1"
            ;;
    esac
done

if [[ "$(uname -s)" != Linux && "${NODEYARD_INSTALL_ANY_OS:-0}" != 1 ]]; then
    die "nodeyard runs on Linux machines only." \
        "To set up Linux machines from a Mac or another computer, use remote-install.sh instead."
fi
if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 3))); then
    die "nodeyard needs bash 4.3 or newer (this is ${BASH_VERSION})." "Upgrade bash with your package manager."
fi
if [[ "$DRY_RUN" -eq 0 && "${EUID}" -ne 0 && ! -w "$PREFIX" ]]; then
    die "Installing to ${PREFIX} needs root." "Run it with sudo: curl -fsSL https://raw.githubusercontent.com/${REPO}/main/install.sh | sudo bash"
fi

DEST="${PREFIX}/lib/nodeyard"
BIN="${PREFIX}/bin"
MANIFEST="${DEST}/.install-manifest"

run() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf '[dry-run] would run: %s\n' "$*" >&2
        return 0
    fi
    "$@"
}

confirm() {
    [[ "$YES" -eq 1 || "$DRY_RUN" -eq 1 ]] && return 0
    if [[ ! -t 0 ]]; then
        # Piped from curl: stdin is the script, so ask on the terminal.
        [[ -r /dev/tty ]] || die "Confirmation needed: $1" "Re-run with --yes."
        local reply
        printf '%s [Y/n] ' "$1" >/dev/tty
        read -r reply </dev/tty || reply=n
        [[ -z "$reply" || "$reply" =~ ^[Yy] ]]
        return
    fi
    local reply
    read -r -p "$1 [Y/n] " reply || reply=n
    [[ -z "$reply" || "$reply" =~ ^[Yy] ]]
}

source_version() { # DIR -> version from its base.sh
    sed -n 's/^NY_VERSION="\(.*\)"$/\1/p' "$1/lib/core/base.sh" 2>/dev/null | head -n1
}

TMP="$(mktemp -d "${TMPDIR:-/tmp}/nodeyard-install.XXXXXXXX")"
trap 'rm -rf "$TMP"' EXIT

# --- get the files ---------------------------------------------------------------

if [[ -n "$FROM_DIR" ]]; then
    [[ -f "${FROM_DIR}/bin/nodeyard" && -d "${FROM_DIR}/lib" ]] ||
        die "${FROM_DIR} doesn't look like nodeyard (no bin/nodeyard)." "Point --from-dir at an unpacked source archive or a git checkout."
    SRC="$(cd "$FROM_DIR" && pwd -P)"
    VERSION="$(source_version "$SRC")"
    if [[ -z "$SOURCE_COMMIT" && -d "${SRC}/.git" ]] && command -v git >/dev/null 2>&1; then
        SOURCE_COMMIT="$(git -C "$SRC" rev-parse HEAD 2>/dev/null || true)"
    fi
else
    command -v curl >/dev/null 2>&1 || die "curl is needed to download nodeyard." "Install curl with your package manager, then try again."
    command -v tar >/dev/null 2>&1 || die "tar is needed to unpack nodeyard." "Install tar with your package manager, then try again."
    case "$(uname -m)" in
        x86_64 | amd64 | aarch64 | arm64) ;;
        *) die "There is no nodeyard build for $(uname -m) yet (only amd64 and arm64)." "Install from a git checkout: git clone https://github.com/${REPO} && sudo bash nodeyard/install.sh --from-dir nodeyard" ;;
    esac
    SOURCE_COMMIT="$(curl -fsSL --proto '=https' --tlsv1.2 --retry 3 --connect-timeout 15 \
        "https://api.github.com/repos/${REPO}/commits/${REF}" 2>/dev/null |
        sed -n 's/^[[:space:]]*"sha": "\([0-9a-f]\{40\}\)".*/\1/p' | head -n1 || true)"
    [[ "$SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]] ||
        die "Could not find source ref ${REF} for ${REPO}." "Check this machine's internet connection and the repository's main branch."
    step "Downloading nodeyard source at ${SOURCE_COMMIT:0:12}"
    curl -fsSL --proto '=https' --tlsv1.2 --retry 3 --connect-timeout 15 \
        -o "${TMP}/source.tar.gz" "https://github.com/${REPO}/archive/${SOURCE_COMMIT}.tar.gz" ||
        die "Could not download nodeyard source at ${SOURCE_COMMIT:0:12}." "Check this machine's internet connection, then try again."
    mkdir -p "${TMP}/src"
    tar -xzf "${TMP}/source.tar.gz" -C "${TMP}/src"
    SRC="$(find "${TMP}/src" -maxdepth 3 -path '*/bin/nodeyard' -print -quit | sed 's#/bin/nodeyard$##')"
    [[ -n "$SRC" && -f "${SRC}/install.sh" ]] || die "The source archive looks incomplete." "Report it at https://github.com/${REPO}/issues"
    actual_version="$(source_version "$SRC")"
    [[ -n "$actual_version" ]] || die "Could not read nodeyard's version from the source archive."
    if [[ -n "$VERSION" && "$VERSION" != "$actual_version" ]]; then
        die "Tag v${VERSION} contains nodeyard ${actual_version}." "Use the version that is in that source tag."
    fi
    VERSION="$actual_version"
fi
[[ -n "$VERSION" ]] || die "Could not tell which nodeyard version this is."
if [[ -n "$SOURCE_COMMIT" && ! "$SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
    die "Invalid source commit metadata." "Re-run the installer from a clean source checkout or archive."
fi

INSTALLED="$(source_version "$DEST" || true)"
if [[ "$INSTALLED" == "$VERSION" && "$FORCE" -eq 0 ]]; then
    say "nodeyard ${VERSION} is already installed in ${DEST}; nothing to do (use --force to reinstall)."
    exit 0
fi

if [[ -n "$INSTALLED" ]]; then
    say "This will update nodeyard ${INSTALLED} -> ${VERSION} in ${DEST}."
else
    say "This will install nodeyard ${VERSION} in ${DEST} and link ${BIN}/nodeyard."
fi
confirm "Continue?" || {
    say "Cancelled; nothing was changed."
    exit 0
}

# --- install -------------------------------------------------------------------

step "Installing files"
new="${DEST}.new.$$"
run mkdir -p "${PREFIX}/lib" "$BIN"
run rm -rf "$new"
run mkdir -p "$new"
for item in bin lib share completions yardcode install.sh uninstall.sh remote-install.sh LICENSE CHANGELOG.md README.md; do
    [[ -e "${SRC}/${item}" ]] && run cp -R "${SRC}/${item}" "${new}/"
done
if [[ -n "$SOURCE_COMMIT" ]]; then
    printf '%s\n' "$SOURCE_COMMIT" >"${TMP}/source-commit"
    run cp "${TMP}/source-commit" "${new}/.source-commit"
fi
run chmod -R u=rwX,go=rX "$new"
run chmod 0755 "${new}/bin/nodeyard"
[[ -f "${new}/share/nodeyard/demo/shim.sh" || "$DRY_RUN" -eq 1 ]] && run chmod 0755 "${new}/share/nodeyard/demo/shim.sh"
[[ -f "${new}/yardcode/bin/yardcode" ]] && run chmod 0755 "${new}/yardcode/bin/yardcode"
# Keep the record of what earlier installs linked, so uninstall still finds it.
if [[ -f "$MANIFEST" ]]; then run cp "$MANIFEST" "${new}/.install-manifest"; fi
if [[ -d "$DEST" ]]; then
    run mv "$DEST" "${DEST}.old.$$"
fi
run mv "$new" "$DEST"
run rm -rf "${DEST}.old.$$"

# record PATH -- remember something we created, for uninstall.sh
record() {
    [[ "$DRY_RUN" -eq 1 ]] && return 0
    grep -qxF "$1" "$MANIFEST" 2>/dev/null || printf '%s\n' "$1" >>"$MANIFEST"
}

# link TARGET LINK -- create a symlink unless LINK is something else we
# shouldn't touch.
link() {
    local target="$1" lnk="$2"
    if [[ -L "$lnk" ]]; then
        run ln -sfn "$target" "$lnk"
    elif [[ -e "$lnk" ]]; then
        return 1
    else
        run ln -s "$target" "$lnk"
    fi
    record "$lnk"
}

link "${DEST}/bin/nodeyard" "${BIN}/nodeyard" || die "${BIN}/nodeyard exists and is not a link; nodeyard won't overwrite it." "Move it aside and run the installer again."

# The old single-file k3s-manager becomes an alias for nodeyard.
if [[ -f "${BIN}/k3s-manager" && ! -L "${BIN}/k3s-manager" ]] && grep -q 'k3s-manager' "${BIN}/k3s-manager" 2>/dev/null; then
    say "Found the old k3s-manager script; keeping it as ${BIN}/k3s-manager.legacy and pointing k3s-manager at nodeyard."
    run mv "${BIN}/k3s-manager" "${BIN}/k3s-manager.legacy"
    record "${BIN}/k3s-manager.legacy"
fi
link "${DEST}/bin/nodeyard" "${BIN}/k3s-manager" || say "Left ${BIN}/k3s-manager alone (not ours)."

# The terminal AI agent (also installable on its own: yardcode/install.sh).
if [[ -f "${DEST}/yardcode/bin/yardcode" || "$DRY_RUN" -eq 1 ]]; then
    link "${DEST}/yardcode/bin/yardcode" "${BIN}/yardcode" || say "Left ${BIN}/yardcode alone (not ours)."
fi

# Some distros' sudo (e.g. RHEL/Rocky/Alma) don't search /usr/local/bin, so
# 'sudo nodeyard' would fail; give those a /usr/bin link too.
if [[ "$PREFIX" == /usr/local ]] && grep -hqsE '^[^#]*secure_path' /etc/sudoers /etc/sudoers.d/* &&
    ! grep -hsE '^[^#]*secure_path' /etc/sudoers /etc/sudoers.d/* | grep -q '/usr/local/bin'; then
    link "${DEST}/bin/nodeyard" /usr/bin/nodeyard || true
    [[ -f "${DEST}/yardcode/bin/yardcode" ]] && { link "${DEST}/yardcode/bin/yardcode" /usr/bin/yardcode || true; }
fi

# Shell completion, where the system has a place for it.
for d in /usr/share/bash-completion/completions /etc/bash_completion.d; do
    if [[ -d "$d" ]]; then
        link "${DEST}/completions/nodeyard.bash" "${d}/nodeyard" || true
        break
    fi
done
for d in /usr/share/zsh/site-functions /usr/share/zsh/vendor-completions; do
    if [[ -d "$d" ]]; then
        link "${DEST}/completions/_nodeyard" "${d}/_nodeyard" || true
        break
    fi
done

if [[ "$NO_DEPS" -eq 0 && "$DRY_RUN" -eq 0 && "${EUID}" -eq 0 ]]; then
    step "Checking required packages"
    "${BIN}/nodeyard" deps --feature core --install --yes >&2 ||
        say "Some required packages could not be installed; run 'sudo nodeyard deps --install' to see why."
fi

say ""
if [[ "$DRY_RUN" -eq 1 ]]; then
    say "Dry run: nothing was changed."
else
    say "nodeyard ${VERSION} is installed."
    say "  Start the guided menu:   sudo nodeyard"
    say "  Check this machine:      sudo nodeyard doctor"
    say "  Try it without hardware: nodeyard --demo"
    say "  Terminal AI agent:       yardcode login   (then: yardcode)"
fi
