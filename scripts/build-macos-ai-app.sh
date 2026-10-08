#!/bin/sh
set -eu

repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
project="$repo/macos/NodeyardAI"
app=${1:-"$repo/dist/Nodeyard AI.app"}
case "$app" in
    *.app) ;;
    *)
        printf '%s\n' 'The output path must end in .app.' >&2
        exit 1
        ;;
esac

if ! command -v swift >/dev/null 2>&1; then
    printf '%s\n' 'Swift is required. Install the Xcode Command Line Tools, then retry.' >&2
    exit 1
fi

sdk=$(/usr/bin/xcrun --sdk macosx15.4 --show-sdk-path 2>/dev/null || /usr/bin/xcrun --sdk macosx --show-sdk-path)
arch=$(uname -m)
case "$arch" in arm64 | x86_64) ;; *)
    printf 'Unsupported Mac architecture: %s\n' "$arch" >&2
    exit 1
    ;;
esac
build="$project/.build/direct"
mkdir -p "$build"
swiftc -O -sdk "$sdk" -target "$arch-apple-macosx13.0" -swift-version 6 -module-cache-path "$build/ModuleCache" \
    "$project"/Sources/NodeyardAI/*.swift -o "$build/NodeyardAI"
binary="$build/NodeyardAI"
test -x "$binary" || {
    printf '%s\n' 'Swift build did not produce the app executable.' >&2
    exit 1
}

rm -rf "$app"
mkdir -p "$app/Contents/MacOS"
cp "$binary" "$app/Contents/MacOS/NodeyardAI"
resources="$app/Contents/Resources"
mkdir -p "$resources"
iconset="$resources/AppIcon.iconset"
icon_builder="$build/build-nodeyard-ai-icon"
swiftc -O -sdk "$sdk" -target "$arch-apple-macosx13.0" -swift-version 6 -module-cache-path "$build/ModuleCache" \
    "$repo/scripts/build-nodeyard-ai-icon.swift" -o "$icon_builder"
"$icon_builder" "$iconset"
/usr/bin/iconutil -c icns "$iconset" -o "$resources/AppIcon.icns"
rm -rf "$iconset"
cat >"$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key><string>en</string>
  <key>CFBundleDisplayName</key><string>Nodeyard AI</string>
  <key>CFBundleExecutable</key><string>NodeyardAI</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleIconName</key><string>AppIcon</string>
  <key>CFBundleIdentifier</key><string>com.nodeyard.ai</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>Nodeyard AI</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsArbitraryLoads</key><true/></dict>
</dict>
</plist>
PLIST
printf 'Built %s\n' "$app"
