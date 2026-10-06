#!/usr/bin/env bash
# Prepare a release tag: checks the version, changelog, lint and tests, then
# creates an annotated tag. Pushing the tag starts the release workflow.
# Usage: scripts/release.sh VERSION
set -euo pipefail

version="${1:?usage: release.sh VERSION}"
root="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$root"

[[ -z "$(git status --porcelain)" ]] || {
    echo "The working tree has uncommitted changes; commit them first." >&2
    exit 1
}
[[ "$(sed -n 's/^NY_VERSION="\(.*\)"$/\1/p' lib/core/base.sh)" == "$version" ]] || {
    echo "Set NY_VERSION=\"${version}\" in lib/core/base.sh first." >&2
    exit 1
}
grep -q "^## \[${version}\] - " CHANGELOG.md || {
    echo "Add a '## [${version}] - YYYY-MM-DD' section to CHANGELOG.md first." >&2
    exit 1
}
git rev-parse -q --verify "refs/tags/v${version}" >/dev/null && {
    echo "Tag v${version} already exists." >&2
    exit 1
}
make lint test
notes="$(awk -v v="$version" '$0 ~ "^## \\[" v "\\]" {on=1; next} /^## \[/ {on=0} on' CHANGELOG.md)"
git tag -a "v${version}" -m "nodeyard ${version}" -m "$notes"
echo "Tagged v${version}. Publish it with: git push origin v${version}"
