#!/usr/bin/env bash
# Build yardcode as ONE executable file (a Python zipapp): copy it anywhere on a Mac or Linux machine with Python 3.8+.
# Usage: scripts/build-yardcode.sh [OUTPUT]   (default: dist/yardcode)
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd -P)"
out="${1:-${root}/dist/yardcode}"
mkdir -p "$(dirname "$out")"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

cp -R "${root}/yardcode/src/yardcode" "${stage}/yardcode"
find "$stage" -name '__pycache__' -type d -prune -exec rm -rf {} +
cat >"${stage}/__main__.py" <<'PY'
import sys

from yardcode.cli import main

sys.exit(main())
PY
python3 -m zipapp "$stage" -o "$out" -p "/usr/bin/env python3" -c
chmod 0755 "$out"
"$out" --version
echo "Built ${out} ($(wc -c <"$out" | tr -d ' ') bytes)"
