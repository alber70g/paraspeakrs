#!/usr/bin/env bash
# Builds packages/recorder-macos/build/Paraspeakrs Recorder.app for this Mac.
#
# Signed ad hoc, with a designated requirement on the bundle identifier
# instead of the default cdhash, so a rebuild keeps the same code identity.
# Whether macOS then keeps the microphone and system-audio grants across
# rebuilds is unverified; if it asks again after every build, sign with a
# real certificate: CODESIGN_IDENTITY="Apple Development: ..." $0
set -euo pipefail

root="$(cd "$(dirname "$0")/../packages/recorder-macos" && pwd)"
identity="${CODESIGN_IDENTITY:--}"
bundle_id="io.github.alber70g.paraspeakrs-recorder"
app="$root/build/Paraspeakrs Recorder.app"

swift build --package-path "$root" -c release --product ParaspeakrsRecorder
bin="$(swift build --package-path "$root" -c release --show-bin-path)/ParaspeakrsRecorder"

rm -rf "$app"
mkdir -p "$app/Contents/MacOS"
cp "$bin" "$app/Contents/MacOS/ParaspeakrsRecorder"
cp "$root/Sources/ParaspeakrsRecorder/Info.plist" "$app/Contents/Info.plist"

if [[ "$identity" == "-" ]]; then
  codesign --force --sign - --identifier "$bundle_id" \
    --requirements "=designated => identifier \"$bundle_id\"" "$app"
else
  codesign --force --sign "$identity" --identifier "$bundle_id" "$app"
fi

echo "built $app"
echo "run:   open \"$app\""
