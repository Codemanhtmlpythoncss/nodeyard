#!/bin/sh
# shellcheck disable=SC2292  # plain POSIX sh on purpose: macOS bash 3.2 and Debian's dash have no reliable [[ ]]
# Install (or update, or remove) yardcode: the terminal AI agent for your own model API.
# Works on macOS and Linux with plain sh; needs Python 3.8+ and nothing else.
#
#   curl -fsSL https://raw.githubusercontent.com/Codemanhtmlpythoncss/nodeyard/main/yardcode/install.sh | sh
#
# It installs for you alone (~/.local) unless you run it as root (then /usr/local), so no sudo is needed.
#
# Options:
#   --prefix DIR     install under DIR (programs in DIR/lib/yardcode, the command in DIR/bin)
#   --from-dir DIR   install from a checkout of the nodeyard repository (or an unpacked yardcode folder)
#   --ref REF        install this branch or tag of the repository (default: main)
#   --uninstall      remove yardcode (your settings and conversations are kept)
#   --force          reinstall even if this version is already installed
#   --yes, -y        don't ask questions
#   --help, -h       this text

set -eu

REPO="${YARDCODE_REPO:-Codemanhtmlpythoncss/nodeyard}"
REF="main"
PREFIX=""
FROM_DIR=""
UNINSTALL=0
FORCE=0
YES=0

say() { printf '%s\n' "$*" >&2; }
step() { printf '> %s\n' "$*" >&2; }
die() {
    printf 'Error: %s\n' "$1" >&2
    [ -n "${2:-}" ] && printf '  Fix: %s\n' "$2" >&2
    exit 1
}

usage() {
    sed -n '2,20p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//' || true
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)
            [ $# -ge 2 ] || die "--prefix needs a folder"
            PREFIX="$2"
            shift 2
            ;;
        --from-dir)
            [ $# -ge 2 ] || die "--from-dir needs a folder"
            FROM_DIR="$2"
            shift 2
            ;;
        --ref)
            [ $# -ge 2 ] || die "--ref needs a branch or tag"
            REF="$2"
            shift 2
            ;;
        --uninstall)
            UNINSTALL=1
            shift
            ;;
        --force)
            FORCE=1
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

# --- where to put it -------------------------------------------------------------------------------

if [ -z "$PREFIX" ]; then
    if [ "$(id -u)" -eq 0 ]; then
        PREFIX="/usr/local"
    else
        PREFIX="${HOME}/.local"
    fi
fi
PREFIX="${PREFIX%/}"
LIB="${PREFIX}/lib/yardcode"
BIN="${PREFIX}/bin"
LINK="${BIN}/yardcode"

if [ "$UNINSTALL" -eq 1 ]; then
    removed=0
    if [ -L "$LINK" ] || [ -f "$LINK" ]; then
        rm -f "$LINK"
        removed=1
    fi
    if [ -d "$LIB" ]; then
        rm -rf "$LIB"
        removed=1
    fi
    if [ "$removed" -eq 1 ]; then
        say "yardcode removed from ${PREFIX}."
    else
        say "yardcode isn't installed under ${PREFIX}."
    fi
    say "Your settings (~/.config/yardcode) and conversations (~/.local/share/yardcode) were kept; delete those folders to remove them too."
    exit 0
fi

# --- Python ------------------------------------------------------------------------------------------

PY=""
for cand in python3 python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
        PY="$cand"
        break
    fi
done
if [ -z "$PY" ]; then
    case "$(uname -s)" in
        Darwin) hint="brew install python   (or install Xcode's command line tools: xcode-select --install)" ;;
        *) hint="sudo apt install python3   (or: sudo dnf install python3 / sudo pacman -S python)" ;;
    esac
    die "yardcode needs Python 3.8 or newer, and none was found." "$hint"
fi

# --- get the files --------------------------------------------------------------------------------------

TMP="$(mktemp -d "${TMPDIR:-/tmp}/yardcode-install.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT INT TERM

SRC=""
if [ -n "$FROM_DIR" ]; then
    d="$(cd "$FROM_DIR" && pwd -P)"
    for cand in "$d/yardcode" "$d"; do
        if [ -f "$cand/bin/yardcode" ] && [ -d "$cand/src/yardcode" ]; then
            SRC="$cand"
            break
        fi
    done
    [ -n "$SRC" ] || die "${FROM_DIR} doesn't contain yardcode (no bin/yardcode and src/yardcode)." "Point --from-dir at a nodeyard checkout."
else
    if command -v curl >/dev/null 2>&1; then
        fetch() { curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$2" "$1"; }
    elif command -v wget >/dev/null 2>&1; then
        fetch() { wget -q -O "$2" "$1"; }
    else
        die "curl or wget is needed to download yardcode." "Install curl, or use --from-dir with a checkout."
    fi
    command -v tar >/dev/null 2>&1 || die "tar is needed to unpack yardcode."
    step "Downloading yardcode (${REF})"
    fetch "https://codeload.github.com/${REPO}/tar.gz/${REF}" "${TMP}/src.tgz" ||
        die "Could not download ${REPO} (${REF})." "Check the internet connection, or use --from-dir with a checkout."
    mkdir -p "${TMP}/src"
    tar -xzf "${TMP}/src.tgz" -C "${TMP}/src" || die "The download looks damaged."
    for cand in "${TMP}"/src/*/yardcode; do
        if [ -f "$cand/bin/yardcode" ]; then
            SRC="$cand"
            break
        fi
    done
    [ -n "$SRC" ] || die "The download has no yardcode folder." "Is ${REPO} the right repository?"
fi

version="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "${SRC}/src/yardcode/__init__.py" | head -n 1)"
[ -n "$version" ] || die "Could not tell which yardcode version this is."

installed=""
if [ -f "${LIB}/src/yardcode/__init__.py" ]; then
    installed="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "${LIB}/src/yardcode/__init__.py" | head -n 1)"
fi
if [ "$installed" = "$version" ] && [ "$FORCE" -eq 0 ] && [ -L "$LINK" ]; then
    say "yardcode ${version} is already installed (${LINK}); nothing to do. Use --force to reinstall."
    exit 0
fi

# --- install ---------------------------------------------------------------------------------------------

if [ -n "$installed" ]; then
    step "Updating yardcode ${installed} -> ${version} in ${LIB}"
else
    step "Installing yardcode ${version} in ${LIB}"
fi
mkdir -p "${PREFIX}/lib" "$BIN" || die "Can't write to ${PREFIX}." "Run with sudo, or install for yourself: sh install.sh --prefix \$HOME/.local"
new="${LIB}.new.$$"
rm -rf "$new"
mkdir -p "${new}/bin" "${new}/src"
cp "${SRC}/bin/yardcode" "${new}/bin/yardcode"
cp -R "${SRC}/src/yardcode" "${new}/src/yardcode"
find "${new}" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
[ -f "${SRC}/README.md" ] && cp "${SRC}/README.md" "${new}/README.md"
chmod 0755 "${new}/bin/yardcode"
# the launcher uses whichever python3 is on PATH; make it use the one we checked if that is a specific version
if [ "$PY" != python3 ]; then
    py_path="$(command -v "$PY")"
    sed "1s|.*|#!${py_path}|" "${new}/bin/yardcode" >"${new}/bin/yardcode.tmp" && mv "${new}/bin/yardcode.tmp" "${new}/bin/yardcode" && chmod 0755 "${new}/bin/yardcode"
fi
[ -d "$LIB" ] && mv "$LIB" "${LIB}.old.$$"
mv "$new" "$LIB"
rm -rf "${LIB}.old.$$"
if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
    die "${LINK} exists and isn't a link; yardcode won't overwrite it." "Move it aside and run the installer again."
fi
ln -sfn "${LIB}/bin/yardcode" "$LINK"

"$LINK" --version >/dev/null 2>&1 || die "The installed program doesn't start." "Run it directly to see why: ${LINK} --version"

say ""
say "yardcode ${version} is installed: ${LINK}"
case ":${PATH}:" in
    *":${BIN}:"*) ;;
    *)
        say ""
        say "  ${BIN} isn't on your PATH yet. Add it:"
        case "${SHELL:-}" in
            */zsh) say "    echo 'export PATH=\"${BIN}:\$PATH\"' >> ~/.zshrc && exec zsh" ;;
            */fish) say "    fish_add_path ${BIN}" ;;
            *) say "    echo 'export PATH=\"${BIN}:\$PATH\"' >> ~/.profile && . ~/.profile" ;;
        esac
        ;;
esac
say ""
say "  Connect it to your model API:   yardcode login"
say "  Start working:                  yardcode"
say "  One-off question:               yardcode -p \"what does this project do?\""
_unused="$YES"
