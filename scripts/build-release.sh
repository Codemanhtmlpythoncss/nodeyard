#!/usr/bin/env bash
# Build release tarballs (one per architecture) and SHA256SUMS into dist/.
# Usage: scripts/build-release.sh VERSION
set -euo pipefail

version="${1:?usage: build-release.sh VERSION}"
root="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$root"

src_version="$(sed -n 's/^NY_VERSION="\(.*\)"$/\1/p' lib/core/base.sh)"
[[ "$src_version" == "$version" ]] || {
    echo "lib/core/base.sh says ${src_version}, not ${version}; bump NY_VERSION first." >&2
    exit 1
}
grep -q "^## \[${version}\]" CHANGELOG.md || {
    echo "CHANGELOG.md has no '## [${version}]' section." >&2
    exit 1
}

# No macOS extended attributes or ._ files in the archives.
export COPYFILE_DISABLE=1
tar_opts=()
tar --help 2>&1 | grep -q -- '--no-xattrs' && tar_opts+=(--no-xattrs)

rm -rf dist
mkdir -p dist/stage
stage="dist/stage/nodeyard-${version}"
mkdir -p "$stage"
# Only tracked files go into a release: nothing generated or local.
git archive --format=tar HEAD bin lib share completions yardcode install.sh uninstall.sh LICENSE README.md CHANGELOG.md |
    tar -x -C "$stage"
[[ -f remote-install.sh ]] && git archive --format=tar HEAD remote-install.sh | tar -x -C "$stage"

# The same scripts serve both architectures today; the per-architecture
# names leave room for the compiled agent added in a later release.
for arch in amd64 arm64; do
    tar "${tar_opts[@]+"${tar_opts[@]}"}" --owner=0 --group=0 --numeric-owner -czf "dist/nodeyard-${version}-linux-${arch}.tar.gz" -C dist/stage "nodeyard-${version}" 2>/dev/null ||
        tar "${tar_opts[@]+"${tar_opts[@]}"}" -czf "dist/nodeyard-${version}-linux-${arch}.tar.gz" -C dist/stage "nodeyard-${version}"
done
cp install.sh dist/install.sh
# yardcode on its own: one executable file (needs only Python 3.8+) and its installer.
scripts/build-yardcode.sh dist/yardcode
cp yardcode/install.sh dist/install-yardcode.sh
rm -rf dist/stage
(
    cd dist
    if command -v sha256sum >/dev/null; then sha256sum ./*.tar.gz install.sh install-yardcode.sh yardcode; else shasum -a 256 ./*.tar.gz install.sh install-yardcode.sh yardcode; fi |
        sed 's#  \./#  #' >SHA256SUMS
)
echo "Built:"
ls -l dist
