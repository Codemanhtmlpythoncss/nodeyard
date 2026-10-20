#!/bin/sh
set -eu

CLI_URL=${PWNFORGE_CLI_URL:-https://raw.githubusercontent.com/Codemanhtmlpythoncss/nodeyard/video-player/share/nodeyard/dashboard/web/pwnforge-lab-access-macos.sh}
DEST=/usr/local/bin/pwnforge
TMP=$(mktemp "${TMPDIR:-/tmp}/pwnforge-cli.XXXXXX")
trap 'rm -f "$TMP"' EXIT HUP INT TERM

command -v curl >/dev/null 2>&1 || { echo "curl is required." >&2; exit 1; }
curl -fsSL "$CLI_URL" -o "$TMP"
grep -q 'PWNFORGE_LAB_ACCESS_CLI=macOS' "$TMP" || { echo "Downloaded file is not the PwnForge macOS CLI." >&2; exit 1; }

if [ -e "$DEST" ] && ! grep -q 'PWNFORGE_LAB_ACCESS_CLI=' "$DEST" 2>/dev/null; then
  echo "$DEST already exists and is not managed by PwnForge; leaving it unchanged." >&2
  exit 1
fi
EXISTING=$(command -v pwnforge 2>/dev/null || true)
if [ -n "$EXISTING" ] && [ "$EXISTING" != "$DEST" ] && ! grep -q 'PWNFORGE_LAB_ACCESS_CLI=' "$EXISTING" 2>/dev/null; then
  echo "An unrelated pwnforge command is already on PATH at $EXISTING; leaving it unchanged." >&2
  echo "Use the PwnForge CLI already installed there, or resolve the command-name conflict first." >&2
  exit 1
fi
if [ "$(id -u)" -eq 0 ]; then
  install -d -o root -g wheel -m 0755 /usr/local/bin
  install -o root -g wheel -m 0755 "$TMP" "$DEST"
else
  command -v sudo >/dev/null 2>&1 || { echo "Run as root or install sudo." >&2; exit 1; }
  sudo install -d -o root -g wheel -m 0755 /usr/local/bin
  sudo install -o root -g wheel -m 0755 "$TMP" "$DEST"
fi

echo "Installed $DEST"
echo "Next: pwnforge install <lab-CIDR> <server-LAN-IP>"
echo "Then: pwnforge status"
