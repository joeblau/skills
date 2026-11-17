#!/bin/sh
# Explicit user-level build; no iCloud login or administrator access is requested.
set -eu
umask 077
if [ "$(uname -s)" != Darwin ]; then
    printf '%s\n' 'The iCloud helper requires macOS.' >&2
    exit 1
fi
cmm_cloud_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cmm_cloud_destination=${CLEANMAC_CLOUD_PATH:-"$cmm_cloud_dir/bin/cleanmac-cloud"}
if [ "$#" -gt 0 ]; then
    if [ "$#" -ne 2 ] || [ "$1" != --output ]; then
        printf '%s\n' 'Usage: build-cloud.sh [--output /absolute/path/cleanmac-cloud]' >&2
        exit 1
    fi
    cmm_cloud_destination=$2
fi
case "$cmm_cloud_destination" in /*) ;; *) printf '%s\n' 'An absolute output path is required.' >&2; exit 1 ;; esac
cmm_cloud_output=$(dirname -- "$cmm_cloud_destination")
if [ -L "$cmm_cloud_output" ] || [ -L "$cmm_cloud_destination" ]; then
    printf '%s\n' 'Symbolic-link output destinations are not supported.' >&2
    exit 1
fi
mkdir -p "$cmm_cloud_output"
cmm_cloud_temporary=$(mktemp "$cmm_cloud_output/.cleanmac-cloud.XXXXXX")
trap 'rm -f "$cmm_cloud_temporary"' EXIT HUP INT TERM
xcrun swiftc -swift-version 6 -O "$cmm_cloud_dir/CloudItems.swift" -o "$cmm_cloud_temporary"
chmod 700 "$cmm_cloud_temporary"
codesign --force --sign - "$cmm_cloud_temporary" >/dev/null
mv -f "$cmm_cloud_temporary" "$cmm_cloud_destination"
trap - EXIT HUP INT TERM
"$cmm_cloud_destination" info
