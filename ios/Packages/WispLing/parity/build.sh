#!/bin/bash
# Build WispLing with xcodebuild (needed so MLX's default.metallib is compiled).
#
#   parity/build.sh macos <work-dir>   # ling-parity for macOS -> <work-dir>/dd/Build/Products/Release/ling-parity
#   parity/build.sh ios   <work-dir>   # WispLing library for generic iOS device (no signing)
#
# <work-dir> holds DerivedData (dd/) and the SwiftPM checkouts (spm/).
set -euo pipefail
target="${1:?macos|ios}"
work="${2:?work dir}"
pkg="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$work"
cd "$pkg"
common=(-derivedDataPath "$work/dd" -clonedSourcePackagesDirPath "$work/spm" -jobs 4
        -skipMacroValidation -skipPackagePluginValidation)
case "$target" in
  macos)
    xcodebuild -scheme ling-parity -destination 'platform=macOS' -configuration Release \
      "${common[@]}" build
    ;;
  ios)
    xcodebuild -scheme WispLing -destination 'generic/platform=iOS' -configuration Release \
      "${common[@]}" CODE_SIGNING_ALLOWED=NO build
    ;;
  *) echo "unknown target $target" >&2; exit 2 ;;
esac
