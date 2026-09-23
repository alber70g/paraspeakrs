# Releasing

## Publishing to PyPI (the normal route)

Two distributions ship from this repository:

| | |
|---|---|
| `paraspeakrs` | the Python package — one wheel for every platform |
| `paraspeakrs-speakrs` | the Rust diarization sidecar — macOS arm64 only |

`paraspeakrs` depends on `paraspeakrs-speakrs` with a **floor** (`>=`), not an exact
pin. An `==` pin on the version being released makes `uv lock` unresolvable inside the
checkout until that version is actually on PyPI — a bootstrap trap on every single
release. The sidecar's interface is its command line rather than a Python API, so
lockstep versions buy nothing; raise the floor only when its CLI or JSON output changes.

`.github/workflows/publish.yml` still publishes the sidecar first and the package
second, so a fresh install always resolves to the matching build.

To cut a release:

1. Bump `version` in **both** `pyproject.toml` and `packages/speakrs-diar/pyproject.toml`
   — the workflow checks the tag against each and fails if either disagrees.
2. Run `uv lock` so the lockfile carries the new version, or the release commit lands
   with a lockfile one version behind.
3. Add a `CHANGELOG.md` entry.
4. Commit as `chore: release X.Y.Z`, then push the tag `vX.Y.Z`. Only `v*` tags trigger
   the publish; pushing `master` on its own releases nothing.

Publishing uses PyPI trusted publishing (OIDC), so there is no API token to rotate. The
`pypi` GitHub environment must be configured as a trusted publisher for both project names.

The sidecar is built on `macos-14` with `--features coreml,blas-static` and
`--auditwheel=repair`. `blas-static` compiles OpenBLAS into the binary, and the repair step
vendors gfortran's runtime (`libgfortran`, `libquadmath`, `libgcc_s`) into the wheel — the
build machine's Homebrew gcc is not on the target machine. The workflow fails the build if
`otool -L` still shows any `/opt/homebrew` or `/usr/local` reference.

## Shipping a zip instead

For a machine that cannot reach PyPI, the project also moves as a **GitHub Release
asset**, not as a file in the repository. The zip is ~18 MB and is almost entirely a compiled Rust binary
plus its dylibs; committing it would add that much permanent history every time it
is regenerated, for an artifact that is reproducible from the source beside it.

## What is in the zip

| | |
|---|---|
| `src/`, `tests/`, `pyproject.toml`, `uv.lock` | tracked source, taken from the **working tree** |
| `bin/speakrs-diar` + `bin/lib/*.dylib` | the diarization sidecar, self-contained |
| `SHIPPING.md` | setup instructions for the target machine |

It deliberately does **not** contain `var/` (previous jobs — meeting audio,
transcripts, speaker embeddings), `test-audio/`, `models/`, or `checkpoints/`.
Those are gitignored, and the zip is built from `git ls-files`, so they cannot be
included by accident. The script also greps the finished zip for those paths and
warns if any appear.

The Parakeet ASR model (~639 MB) is not bundled and must be fetched separately on
the target; the speakrs diarization models (~315 MB) download themselves on first
run from the public `avencera/speakrs-models` repo, with no Hugging Face token.

## Cutting a release

```sh
# 1. Build the zip from the current working tree.
TAG=v0.5.0   # the version being released
scripts/make-shipping-zip.sh /tmp/paraspeakrs-$TAG.zip

# 2. Tag and publish. `gh auth login` first if the token has expired.
git tag -a $TAG -m "<one-line summary>"
git push origin $TAG
gh release create $TAG /tmp/paraspeakrs-$TAG.zip \
  --title "$TAG" \
  --notes "<release notes>. See SHIPPING.md inside the zip for setup."
```

Rebuild and attach a fresh asset rather than editing one in place:

```sh
gh release upload $TAG /tmp/paraspeakrs-$TAG.zip --clobber
```

## Installing on the target machine

```sh
TAG=v0.5.0   # the release to install
gh release download $TAG --repo alber70g/paraspeakrs
unzip paraspeakrs-$TAG.zip
cd paraspeakrs

# A downloaded zip is quarantined; macOS will refuse to run the bundled binary.
xattr -dr com.apple.quarantine .

cat SHIPPING.md    # then follow it
```

`SHIPPING.md` covers uv, ffmpeg, the ASR model, and pointing `SPEAKRS_BIN` at the
bundled binary. If Gatekeeper blocks the binary because the machine's MDM policy
requires notarized executables, it also documents rebuilding from source instead —
that path needs `brew install openblas` and a Rust toolchain.

## Why the binary is bundled rather than rebuilt

`speakrs-diar` links OpenBLAS. Built normally it points at
`/opt/homebrew/opt/openblas/lib/libopenblas.0.dylib` and will not start on a machine
without Homebrew. The build script therefore copies the full dylib closure
(`libopenblas`, `libgfortran`, `libomp`, `libquadmath`, `libgcc_s`) into `bin/lib/`
and rewrites the load paths to `@executable_path/lib`.

Two traps that are worth knowing if this ever needs redoing by hand:

- Bundling only `libopenblas` looks like it works, because `libgfortran`, `libomp`
  and `libquadmath` still resolve from Homebrew **on the build machine**. Check the
  whole closure, not just the direct dependency.
- `dylibbundler` can append the same `LC_RPATH` several times, and dyld refuses to
  load a binary with duplicate rpaths (`duplicate LC_RPATH '@executable_path/lib/'`).
  The script collapses them and re-signs.

`otool -L` output is not proof either way; the script verifies by actually running
the binary, and `DYLD_PRINT_LIBRARIES=1` shows which dylibs really get loaded.

Statically linking OpenBLAS instead (the `blas-static` feature on
`packages/speakrs-diar`) would avoid all of this, but its source build currently
fails against modern gcc.
