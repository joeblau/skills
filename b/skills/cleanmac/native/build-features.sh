#!/bin/sh
# Explicit native app build. No permission request, daemon, or elevation.
set -eu
umask 077
if [ "$(uname -s)" != Darwin ]; then
    printf '%s\n' 'CleanMac native features require macOS.' >&2
    exit 1
fi
if ! command -v xcrun >/dev/null 2>&1 || ! xcrun --find swiftc >/dev/null 2>&1; then
    printf '%s\n' 'Install Apple Command Line Tools or Xcode to build native features.' >&2
    exit 1
fi
cmm_features_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cmm_features_app="$cmm_features_dir/bin/CleanMac Features.app"
if [ "$#" -gt 0 ]; then
    if [ "$#" -ne 2 ] || [ "$1" != --output-app ]; then
        printf '%s\n' 'Usage: build-features.sh [--output-app /absolute/path/CleanMacFeatures.app]' >&2
        exit 1
    fi
    cmm_features_app=$2
fi
case "$cmm_features_app" in /*.app) ;; *) printf '%s\n' 'Choose an absolute .app output path.' >&2; exit 1 ;; esac
if [ -L "$cmm_features_app" ] || [ -L "$(dirname -- "$cmm_features_app")" ] || \
   [ -L "$cmm_features_app/Contents" ] || [ -L "$cmm_features_app/Contents/MacOS" ] || \
   [ -L "$cmm_features_app/Contents/Info.plist" ] || [ -L "$cmm_features_app/Contents/MacOS/cleanmac-features" ]; then
    printf '%s\n' 'Refusing symbolic-link output paths.' >&2
    exit 1
fi
if [ -e "$cmm_features_app/Contents/Info.plist" ]; then
    cmm_features_existing_id=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$cmm_features_app/Contents/Info.plist" 2>/dev/null || true)
    if [ "$cmm_features_existing_id" != com.joeblau.cleanmac.features ]; then
        printf '%s\n' 'Refusing to overwrite an app with another bundle identity.' >&2
        exit 1
    fi
fi
mkdir -p "$cmm_features_app/Contents/MacOS"
cmm_features_temp=$(mktemp "$cmm_features_app/Contents/MacOS/.features.XXXXXX")
trap 'rm -f "$cmm_features_temp"' EXIT HUP INT TERM
xcrun swiftc -swift-version 6 -parse-as-library -target "$(uname -m)-apple-macos13.0" -O "$cmm_features_dir/CleanMacFeatures.swift" -o "$cmm_features_temp"
chmod 700 "$cmm_features_temp"
mv -f "$cmm_features_temp" "$cmm_features_app/Contents/MacOS/cleanmac-features"
cat > "$cmm_features_app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.joeblau.cleanmac.features</string>
<key>CFBundleName</key><string>CleanMac Features</string>
<key>CFBundleExecutable</key><string>cleanmac-features</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>CFBundleShortVersionString</key><string>1.0.0</string>
<key>LSUIElement</key><true/>
<key>LSMinimumSystemVersion</key><string>13.0</string>
<key>NSPhotoLibraryUsageDescription</key><string>Review your selected Photos assets and delete only assets you explicitly approve. Photos controls iCloud synchronization and Recently Deleted recovery.</string>
</dict></plist>
PLIST
codesign --force --sign - "$cmm_features_app" >/dev/null
trap - EXIT HUP INT TERM
"$cmm_features_app/Contents/MacOS/cleanmac-features" info
