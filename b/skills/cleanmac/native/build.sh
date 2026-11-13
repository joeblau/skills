#!/bin/sh
# Explicit local build; this creates no daemon and requests no administrator access.
set -eu
umask 077

if [ "$(uname -s)" != Darwin ]; then
    printf '%s\n' 'CleanMac native Trash support requires macOS.' >&2
    exit 1
fi
if ! command -v xcrun >/dev/null 2>&1 || ! xcrun --find swiftc >/dev/null 2>&1; then
    printf '%s\n' 'Install Apple Command Line Tools or Xcode to build native Trash support.' >&2
    exit 1
fi

cmm_native_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cmm_native_destination=${CLEANMAC_NATIVE_PATH:-"$cmm_native_dir/bin/cleanmac-native"}
if [ "$#" -gt 0 ]; then
    if [ "$#" -ne 2 ] || [ "$1" != --output ]; then
        printf '%s\n' 'Usage: build.sh [--output /absolute/path/cleanmac-native]' >&2
        exit 1
    fi
    cmm_native_destination=$2
fi
case "$cmm_native_destination" in
    /*) ;;
    *) printf '%s\n' 'The native output path must be absolute.' >&2; exit 1 ;;
esac
cmm_native_output=$(dirname -- "$cmm_native_destination")
if [ -L "$cmm_native_output" ]; then
    printf '%s\n' 'Refusing a symbolic-link native output directory.' >&2
    exit 1
fi
if [ -L "$cmm_native_destination" ]; then
    printf '%s\n' 'Refusing a symbolic-link native output file.' >&2
    exit 1
fi
mkdir -p "$cmm_native_output"
cmm_native_temp=$(mktemp "$cmm_native_output/.cleanmac-native.XXXXXX")
trap 'rm -f "$cmm_native_temp"' EXIT HUP INT TERM
xcrun swiftc -swift-version 6 -O "$cmm_native_dir/CleanMacNative.swift" -o "$cmm_native_temp"
chmod 700 "$cmm_native_temp"
if command -v codesign >/dev/null 2>&1; then
    codesign --force --sign - "$cmm_native_temp" >/dev/null
fi
mv -f "$cmm_native_temp" "$cmm_native_destination"
trap - EXIT HUP INT TERM
"$cmm_native_destination" info
