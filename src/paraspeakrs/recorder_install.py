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
from urllib.parse import urlencode

BUNDLE_ID = "io.github.alber70g.paraspeakrs-recorder"
APP_NAME = "Paraspeakrs Recorder.app"
PRODUCT = "ParaspeakrsRecorder"
DEFAULT_DEST = Path("~/Applications")
BUILD_DIR = Path("~/Library/Caches/paraspeakrs/recorder-build")
NEW_ISSUE = "https://github.com/alber70g/paraspeakrs/issues/new"


class CommandFailed(RuntimeError):
    """A command the installer ran failed; unlike a missing prerequisite, worth reporting."""


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
    shutil.copytree(work / "Sources" / PRODUCT / "Sounds", staged / "Contents" / "Resources")

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


_COMPILER_ERROR = re.compile(r"^.+:\d+:\d+: error: ")


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        # `swift build` ends with "Build failed"; the reason is in its `file:line:col: error:` lines.
        output = f"{result.stdout}\n{result.stderr}".splitlines()
        errors = list(dict.fromkeys(line for line in output if _COMPILER_ERROR.match(line)))
        if errors:
            raise CommandFailed(f"{cmd[0]} failed:" + "".join(f"\n  {line}" for line in errors[:20]))
        hint = ""
        if cmd[0] == "codesign" and "errSecInternalComponent" in (result.stderr or ""):
            hint = ("\nThe certificate's chain is incomplete. Install Apple's intermediate: "
                    "https://www.apple.com/certificateauthority/AppleWWDRCAG3.cer")
        raise CommandFailed(f"{cmd[0]} failed: {detail[-1] if detail else result.returncode}{hint}")
    return result.stdout


def issue_url(error: str) -> str:
    """A new GitHub issue with the error and the toolchain filled in. The home
    directory becomes `~`, so the username stays out of a public issue."""
    from . import __version__

    facts = {"paraspeakrs": __version__, "macOS": platform.mac_ver()[0], **_toolchain()}
    log = error.replace(str(Path.home()), "~")[:3000]
    table = "\n".join(f"| {name} | {value} |" for name, value in facts.items())
    body = f"`paraspeakrs install-recorder` failed.\n\n| | |\n|---|---|\n{table}\n\n```\n{log}\n```\n"
    title = f"install-recorder fails (macOS {facts['macOS']}, SDK {facts['SDK']})"
    return f"{NEW_ISSUE}?{urlencode({'title': title, 'body': body})}"


def _toolchain() -> dict[str, str]:
    """The first line of `swift --version` and the SDK version, or "unknown"."""
    def first_line(cmd: list[str]) -> str:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        lines = (result.stdout or result.stderr).strip().splitlines()
        return lines[0] if result.returncode == 0 and lines else "unknown"

    return {"Swift": first_line(["swift", "--version"]), "SDK": first_line(["xcrun", "--show-sdk-version"])}
