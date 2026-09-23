#!/usr/bin/env bash
# Build a zip of this project for another Mac.
#
# Source comes from `git archive`, so anything .gitignore excludes -- var/ (past
# job audio and transcripts), test-audio/, models/, checkpoints/ -- is left out by
# construction rather than by remembering to exclude it. Read the manifest it
# prints before sending the file anywhere.
#
# Usage: scripts/make-shipping-zip.sh [output.zip]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

OUT="${1:-$REPO_ROOT/../parakeet-service-ship-$(date +%Y%m%d).zip}"
STAGE="$(mktemp -d)/parakeet-service"
BINARY="packages/speakrs-diar/target/release/speakrs-diar"

trap 'rm -rf "$(dirname "$STAGE")"' EXIT

if [ ! -x "$BINARY" ]; then
  echo "error: $BINARY not built. Run:" >&2
  echo "  cd packages/speakrs-diar && cargo build --release --features coreml" >&2
  exit 1
fi

mkdir -p "$STAGE"

echo "==> Exporting tracked source (working tree, not HEAD)"
# git ls-files gives the .gitignore-respecting file list -- ignored paths (var/,
# test-audio/, models/, checkpoints/) are untracked, so they cannot leak in. We copy
# from the working tree rather than `git archive HEAD` so uncommitted edits ship too.
git ls-files -z | while IFS= read -r -d '' f; do
  mkdir -p "$STAGE/$(dirname "$f")"
  cp "$f" "$STAGE/$f"
done

echo "==> Bundling speakrs-diar with its dylib closure"
mkdir -p "$STAGE/bin/lib"
cp "$BINARY" "$STAGE/bin/"
dylibbundler -od -b -x "$STAGE/bin/speakrs-diar" -d "$STAGE/bin/lib" -p @executable_path/lib >/dev/null

# dylibbundler can append the same LC_RPATH more than once; dyld refuses to load a
# binary with duplicate rpaths, so collapse them back to one and re-sign.
rpath_count() {
  otool -l "$1" | awk '/LC_RPATH/{f=1} f && / path /{print $2; f=0}' | grep -cx "@executable_path/lib/" || true
}
for f in "$STAGE"/bin/speakrs-diar "$STAGE"/bin/lib/*.dylib; do
  for _ in 1 2 3 4 5; do
    [ "$(rpath_count "$f")" -le 1 ] && break
    install_name_tool -delete_rpath "@executable_path/lib/" "$f" 2>/dev/null || break
  done
  codesign --force -s - "$f" 2>/dev/null || true
done

echo "==> Verifying the bundle has no references outside the zip"
leaked=0
for f in "$STAGE"/bin/speakrs-diar "$STAGE"/bin/lib/*.dylib; do
  if otool -L "$f" | tail -n +2 | grep -qE "/opt/homebrew|/usr/local"; then
    echo "  LEAK: $(basename "$f") still references a local path" >&2
    otool -L "$f" | tail -n +2 | grep -E "/opt/homebrew|/usr/local" >&2
    leaked=1
  fi
done
[ "$leaked" -eq 0 ] || { echo "error: bundle is not self-contained" >&2; exit 1; }

# Prove it actually loads, rather than trusting the load commands.
"$STAGE/bin/speakrs-diar" --help >/dev/null || { echo "error: bundled binary will not run" >&2; exit 1; }

cp "$REPO_ROOT/scripts/SHIPPING.md" "$STAGE/SHIPPING.md" 2>/dev/null || true

echo "==> Manifest (check this before sending)"
(cd "$STAGE" && find . -type f | sed 's|^\./||' | sort | head -40)
echo "    ..."
echo "    files: $(find "$STAGE" -type f | wc -l | tr -d ' ')"

rm -f "$OUT"
(cd "$(dirname "$STAGE")" && zip -qr "$OUT" parakeet-service)
echo "==> Wrote $OUT ($(du -h "$OUT" | cut -f1))"
echo "    Sanity check for excluded data:"
if unzip -l "$OUT" | grep -qE " (var|test-audio|models|checkpoints)/"; then
  echo "    WARNING: zip contains data directories -- inspect before sending" >&2
else
  echo "    ok: no var/, test-audio/, models/ or checkpoints/ in the zip"
fi
