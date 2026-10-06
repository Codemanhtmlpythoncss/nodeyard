#!/usr/bin/env bash
# Fetch the pinned test and lint tools listed in tests/tools.lock into
# .tools/, verifying each download's SHA-256. Safe to re-run.
#   bats, bats-support, bats-assert: every platform
#   the shell linter and formatter: Linux amd64/arm64 (macOS: brew install shellcheck shfmt)
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd -P)"
tools="${root}/.tools"
mkdir -p "${tools}/bin"

case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) platform="linux-amd64" ;;
    Linux-aarch64 | Linux-arm64) platform="linux-arm64" ;;
    *) platform="other" ;;
esac

sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

fetch() { # name version sha url dest
    local tmp
    tmp="$(mktemp)"
    curl -fsSL --proto '=https' --retry 3 -o "$tmp" "$4"
    if [[ "$(sha256 "$tmp")" != "$3" ]]; then
        rm -f "$tmp"
        echo "Checksum mismatch for $1 $2 ($4)" >&2
        exit 1
    fi
    mv "$tmp" "$5"
}

while read -r name version plat sha url; do
    [[ -z "$name" || "$name" == \#* ]] && continue
    [[ "$plat" == any || "$plat" == "$platform" ]] || continue
    stamp="${tools}/.${name}-${version}"
    [[ -e "$stamp" ]] && continue
    echo "Fetching ${name} ${version}"
    case "$name" in
        bats-*)
            fetch "$name" "$version" "$sha" "$url" "${tools}/${name}.tar.gz"
            rm -rf "${tools:?}/${name}"
            mkdir -p "${tools}/${name}"
            tar -xzf "${tools}/${name}.tar.gz" -C "${tools}/${name}" --strip-components=1
            rm -f "${tools}/${name}.tar.gz"
            [[ "$name" == bats-core ]] && ln -sfn "${tools}/bats-core/bin/bats" "${tools}/bin/bats"
            ;;
        shellcheck)
            fetch "$name" "$version" "$sha" "$url" "${tools}/shellcheck.tar.xz"
            tar -xJf "${tools}/shellcheck.tar.xz" -C "$tools"
            mv "${tools}/shellcheck-v${version}/shellcheck" "${tools}/bin/shellcheck"
            rm -rf "${tools}/shellcheck.tar.xz" "${tools}/shellcheck-v${version}"
            ;;
        shfmt)
            fetch "$name" "$version" "$sha" "$url" "${tools}/bin/shfmt"
            chmod +x "${tools}/bin/shfmt"
            ;;
    esac
    : >"$stamp"
done <"${root}/tests/tools.lock"

if [[ "$platform" == other ]]; then
    for t in shellcheck shfmt; do
        command -v "$t" >/dev/null 2>&1 || echo "Note: install ${t} yourself on this platform (e.g. brew install ${t})." >&2
    done
fi
echo "Tools ready in ${tools}"
