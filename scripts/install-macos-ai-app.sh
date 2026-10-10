#!/bin/sh
# shellcheck disable=SC2292  # plain POSIX sh on purpose (macOS ships bash 3.2 as sh's only bash)
# Builds Nodeyard AI from this checkout and installs it in /Applications, replacing the old copy only once the new
# one is built and checked. Chats (~/Library/Application Support/NodeyardAI), settings and Keychain items are not
# touched.
#
#   sh scripts/install-macos-ai-app.sh              build, check, install (a running copy keeps running until you quit it)
#   sh scripts/install-macos-ai-app.sh --relaunch   also quit the running copy and open the new one
#   sh scripts/install-macos-ai-app.sh --dest PATH  install somewhere else (must end in .app)
set -eu

repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
dest="/Applications/Nodeyard AI.app"
relaunch=0
while [ $# -gt 0 ]; do
    case "$1" in
        --relaunch) relaunch=1 ;;
        --dest)
            [ $# -ge 2 ] || {
                printf '%s\n' '--dest needs a path ending in .app' >&2
                exit 2
            }
            dest="$2"
            shift
            ;;
        -h | --help)
            sed -n '2,10p' "$0"
            exit 0
            ;;
        *)
            printf 'Unknown option: %s\n' "$1" >&2
            exit 2
            ;;
    esac
    shift
done
case "$dest" in
    *.app) ;;
    *)
        printf '%s\n' 'The destination must end in .app.' >&2
        exit 2
        ;;
esac

stage=$(mktemp -d "${TMPDIR:-/tmp}/nodeyard-ai-install.XXXXXX")
backup=""
cleanup() {
    rm -rf "$stage"
    if [ -n "$backup" ] && [ -e "$backup" ] && [ ! -e "$dest" ]; then
        mv "$backup" "$dest" && printf 'Put the previous app back at %s\n' "$dest" >&2
    fi
}
trap cleanup EXIT

printf '%s\n' '==> Building'
new="$stage/Nodeyard AI.app"
sh "$repo/scripts/build-macos-ai-app.sh" "$new"

printf '%s\n' '==> Checking the new bundle'
plist="$new/Contents/Info.plist"
/usr/bin/plutil -lint "$plist" >/dev/null
[ "$(/usr/bin/plutil -extract CFBundleIdentifier raw "$plist")" = com.nodeyard.ai ] || {
    printf '%s\n' 'The build has the wrong bundle identifier; not installing it.' >&2
    exit 1
}
exe="$new/Contents/MacOS/$(/usr/bin/plutil -extract CFBundleExecutable raw "$plist")"
if [ ! -x "$exe" ] || ! /usr/bin/file "$exe" | grep -q 'Mach-O'; then
    printf '%s\n' 'The build has no runnable executable; not installing it.' >&2
    exit 1
fi
[ -s "$new/Contents/Resources/AppIcon.icns" ] || {
    printf '%s\n' 'The build has no app icon; not installing it.' >&2
    exit 1
}
/usr/bin/codesign --verify --strict "$new" 2>/dev/null || printf '%s\n' 'warning: the bundle signature did not verify (it still runs locally).' >&2
version=$(/usr/bin/plutil -extract CFBundleShortVersionString raw "$plist")
build=$(/usr/bin/plutil -extract CFBundleVersion raw "$plist")
commit=$(/usr/bin/plutil -extract NodeyardSourceCommit raw "$plist")

running=$(pgrep -f "$dest/Contents/MacOS/" 2>/dev/null || true)
if [ -n "$running" ] && [ "$relaunch" -eq 1 ]; then
    printf '%s\n' '==> Quitting the running Nodeyard AI (chats are saved as you go)'
    for pid in $running; do kill -TERM "$pid" 2>/dev/null || true; done
    i=0
    while [ "$i" -lt 20 ] && pgrep -f "$dest/Contents/MacOS/" >/dev/null 2>&1; do
        sleep 0.5
        i=$((i + 1))
    done
fi

printf 'Installing %s\n' "$dest"
if [ -e "$dest" ]; then
    backup="$(dirname "$dest")/.Nodeyard AI.previous.$$.app"
    mv "$dest" "$backup"
fi
/usr/bin/ditto "$new" "$dest"
installed_commit=$(/usr/bin/plutil -extract NodeyardSourceCommit raw "$dest/Contents/Info.plist" 2>/dev/null || echo none)
if [ "$installed_commit" != "$commit" ] || ! /usr/bin/codesign --verify "$dest" 2>/dev/null; then
    printf '%s\n' 'The installed copy does not match the build; putting the previous app back.' >&2
    rm -rf "$dest"
    exit 1
fi
[ -z "$backup" ] || rm -rf "$backup"
backup=""
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$dest" >/dev/null 2>&1 || true

printf 'Installed Nodeyard AI %s (build %s, commit %s) at %s\n' "$version" "$build" "$commit" "$dest"
if [ "$relaunch" -eq 1 ]; then
    /usr/bin/open "$dest"
    printf '%s\n' 'Opened the new version.'
elif [ -n "$running" ]; then
    printf '%s\n' 'Nodeyard AI is still running the previous version: quit it (Cmd-Q) and open it again to use the new one.'
fi
