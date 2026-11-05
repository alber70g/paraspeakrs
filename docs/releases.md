# Releasing

## Publishing to PyPI (the normal route)

Two distributions ship from this repository:

| | |
|---|---|
| `paraspeakrs` | the Python package — one wheel for every platform |
| `paraspeakrs-speakrs` | the Rust sidecars — `speakrs-diar` for macOS arm64, Linux x86_64 and Windows x64; `paraspeakrs-record` for Linux x86_64 and Windows x64 |

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

The sidecar wheels are built by `.github/workflows/build-speakrs.yml`, which `ci.yml`
runs on every push and `publish.yml` runs for the tag, so a release ships what CI already
built:

| Wheel | Runner | Features | Notes |
|---|---|---|---|
| macOS arm64 | `macos-15` | `coreml` | CoreML models need macOS 15; the smoke test runs both modes |
| Linux x86_64 | `ubuntu-latest`, inside `manylinux_2_28` | `cuda,recorder` | `--auditwheel skip`, so the system ALSA is linked, not vendored |
| Windows x64 | `windows-latest` | `recorder` | static C runtime (`+crt-static`), so no `vcruntime140.dll` |

Every wheel gets the `cpu` backend (the crate default). Each job runs the Rust tests,
`scripts/check-speakrs-wheel.py` (fails on any library the target machine may lack:
Homebrew paths, OpenSSL, the VC++ runtime), and `scripts/smoke-test-speakrs.py`, which
installs the wheel, downloads the models and diarizes a file. The Linux job also records
three seconds through PulseAudio virtual devices.

### Beta releases

A version such as `0.11.0b1` (PEP 440 pre-release) publishes like any other: bump both
`pyproject.toml` files, `uv lock`, push the tag `v0.11.0b1`. pip and uv skip
pre-releases unless asked for one by version, so `uv tool install paraspeakrs` keeps
getting the last stable release.

A tester has to pin **both** packages. `paraspeakrs` depends on
`paraspeakrs-speakrs>=0.2.0`, and a resolver only picks a pre-release of a dependency
when the requirement names one, so pinning `paraspeakrs` alone gets the beta with the
last stable sidecar (on Linux and Windows, none at all):

```sh
uv tool install paraspeakrs==0.11.0b1 --with paraspeakrs-speakrs==0.11.0b1
```

Raising the floor to the beta instead would make `uv lock` unresolvable in the checkout
until the beta is on PyPI, the bootstrap trap the floor exists to avoid.

### The first release with Linux and Windows wheels

Until a `paraspeakrs-speakrs` release with Linux and Windows wheels is on PyPI, the
lockfile names one with only a macOS wheel, so `uv sync` on Linux or Windows cannot
install it. CI therefore syncs with `--no-install-package paraspeakrs-speakrs` (the tests
mock the sidecar). After that release:

1. `uv lock --upgrade-package paraspeakrs-speakrs`, so the lockfile names it.
2. Add `required-environments` under `[tool.uv]` in `pyproject.toml` for the three
   platforms, so `uv lock` can never again settle on a version without their wheels.
3. Raise the `paraspeakrs-speakrs>=` floor to that version: older ones lack the new
   platforms and still link OpenBLAS.
4. Drop `--no-install-package paraspeakrs-speakrs` from `ci.yml` and `publish.yml` if the
   tests should run against the real binary.

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
the target; the speakrs diarization models (about 216 MB with CoreML) download themselves on first
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
that path needs a Rust toolchain.

## Why the binary is bundled rather than rebuilt

Since speakrs 0.7 (paraspeakrs-speakrs after 0.10.0), `speakrs-diar` links no OpenBLAS,
no ONNX Runtime and no OpenSSL; it depends only on macOS system frameworks. The zip still
carries the binary so the target machine needs no Rust toolchain, and the script still
runs `dylibbundler`, which now finds nothing to copy.

Earlier builds linked Homebrew's OpenBLAS and needed its whole dylib closure
(`libopenblas`, `libgfortran`, `libomp`, `libquadmath`, `libgcc_s`) bundled into
`bin/lib/` with load paths rewritten to `@executable_path/lib`. If a dependency like that
ever returns: check the whole closure, not just the direct dependency, because the rest
still resolves from Homebrew on the build machine; and `dylibbundler` can append the same
`LC_RPATH` several times, which dyld refuses to load (the script collapses them and
re-signs). `otool -L` output is not proof either way; the script verifies by actually
running the binary.
