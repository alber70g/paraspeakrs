"""`paraspeakrs install-recorder`: build the macOS meeting recorder and install it.

The recorder is a Swift menu-bar app (``packages/recorder-macos`` in the
repository). Its sources ship inside this package, so anyone who installed
paraspeakrs can build it locally with the Xcode Command Line Tools. An app
built on the machine that runs it is not quarantined, so Gatekeeper does not
block it the way it blocks a downloaded build without a Developer ID.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

BUNDLE_ID = "io.github.alber70g.paraspeakrs-recorder"
APP_NAME = "Paraspeakrs Recorder.app"
PRODUCT = "ParaspeakrsRecorder"
DEFAULT_DEST = Path("~/Applications")
BUILD_DIR = Path("~/Library/Caches/paraspeakrs/recorder-build")


def find_sources() -> Path:
    """The Swift package: inside the installed wheel, or in a source checkout."""
    packaged = Path(__file__).parent / "recorder_macos"
    checkout = Path(__file__).resolve().parents[2] / "packages" / "recorder-macos"
    for candidate in (packaged, checkout):
        if (candidate / "Package.swift").is_file():
            return candidate
    raise RuntimeError("the recorder's Swift sources are missing from this install")


def pick_identity(listing: str) -> str:
    """Choose a signing identity from `security find-identity -v -p codesigning`.

    Developer ID first: it is the only kind Gatekeeper accepts on other Macs.
    Apple Development next: macOS keeps the microphone and system-audio grants
    across rebuilds only when the code identity stays the same. "-" (ad hoc)
    when there is no certificate at all, so a fresh machine can still build.
    """
    names = re.findall(r'^\s*\d+\) [0-9A-F]{40} "(.+)"$', listing, re.MULTILINE)
    for prefix in ("Developer ID Application:", "Apple Development:"):
        for name in names:
            if name.startswith(prefix):
                return name
    return "-"


def install(*, dest_dir: Path, identity: str | None, open_app: bool) -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("the recorder is a macOS app; there is nothing to install on this platform")
    if int(platform.mac_ver()[0].split(".")[0] or 0) < 15:
        raise RuntimeError(f"the recorder needs macOS 15 or later (this is {platform.mac_ver()[0]})")
    swift = shutil.which("swift")
    if swift is None:
        raise RuntimeError("Swift is not installed. Install the Xcode Command Line Tools with: xcode-select --install")

    work = BUILD_DIR.expanduser()
    _copy_sources(find_sources(), work)
    print(f"building the recorder in {work} (the first build takes a minute or two)", file=sys.stderr)
    _run([swift, "build", "--package-path", str(work), "-c", "release", "--product", PRODUCT])
    bin_dir = Path(_run([swift, "build", "--package-path", str(work), "-c", "release", "--show-bin-path"]).strip())

    staged = work / "app" / APP_NAME
    shutil.rmtree(staged, ignore_errors=True)
    (staged / "Contents" / "MacOS").mkdir(parents=True)
    shutil.copy2(bin_dir / PRODUCT, staged / "Contents" / "MacOS" / PRODUCT)
    shutil.copy2(work / "Sources" / PRODUCT / "Info.plist", staged / "Contents" / "Info.plist")

    chosen = identity or pick_identity(_run(["security", "find-identity", "-v", "-p", "codesigning"]))
    _sign(staged, chosen)

    dest = dest_dir.expanduser() / APP_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    # A running copy keeps its old binary mapped; quit it so the new one starts.
    subprocess.run(["osascript", "-e", f'quit app id "{BUNDLE_ID}"'], capture_output=True)
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(staged, dest, symlinks=True)

    executable = _this_executable()
    if executable is not None:
        _run(["defaults", "write", BUNDLE_ID, "paraspeakrsPath", str(executable)])
    print(f"installed {dest}", file=sys.stderr)
    print(f"signed with: {'ad hoc (no certificate found)' if chosen == '-' else chosen}", file=sys.stderr)
    if executable is not None:
        print(f"the recorder will run {executable} after each recording", file=sys.stderr)
    if open_app:
        _run(["open", str(dest)])
    return dest


def _copy_sources(sources: Path, work: Path) -> None:
    """Fresh sources each time, but keep `.build` so a rebuild is incremental."""
    work.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(work / "Sources", ignore_errors=True)
    shutil.copytree(sources / "Sources", work / "Sources")
    shutil.copy2(sources / "Package.swift", work / "Package.swift")


def _sign(app: Path, identity: str) -> None:
    if identity == "-":
        # Pin the designated requirement to the bundle id instead of the
        # default cdhash, which changes on every build.
        _run(["codesign", "--force", "--sign", "-", "--identifier", BUNDLE_ID,
              "--requirements", f'=designated => identifier "{BUNDLE_ID}"', str(app)])
    else:
        _run(["codesign", "--force", "--sign", identity, "--identifier", BUNDLE_ID, str(app)])


def _this_executable() -> Path | None:
    """The `paraspeakrs` running this command, so the app calls the same install."""
    argv0 = Path(sys.argv[0])
    if argv0.name != "paraspeakrs":
        return None
    resolved = argv0 if argv0.is_absolute() else Path(shutil.which(str(argv0)) or argv0).absolute()
    return resolved if os.access(resolved, os.X_OK) else None


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        hint = ""
        if cmd[0] == "codesign" and "errSecInternalComponent" in (result.stderr or ""):
            hint = ("\nThe certificate's chain is incomplete. Install Apple's intermediate: "
                    "https://www.apple.com/certificateauthority/AppleWWDRCAG3.cer")
        raise RuntimeError(f"{cmd[0]} failed: {detail[-1] if detail else result.returncode}{hint}")
    return result.stdout
