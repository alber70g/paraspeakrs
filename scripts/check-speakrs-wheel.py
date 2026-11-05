#!/usr/bin/env python3
"""Fail unless every native binary in a speakrs-diar wheel runs on a clean machine.

The wheel is installed by people who have none of the build machine's libraries,
so each binary may only link what every target system already has:

- macOS (Mach-O): system frameworks and /usr/lib, or files inside the wheel
  (@loader_path, @rpath, @executable_path). Anything under /opt/homebrew or
  /usr/local exists only on the build machine.
- Linux (ELF): the glibc core libraries. Not OpenSSL: its soname differs between
  distributions, so a binary linked against one distribution's libssl fails to
  start on the next. The sidecar downloads over rustls for that reason.
- Windows (PE): Windows' own DLLs. Not the Visual C++ runtime (vcruntime140.dll),
  which many machines lack; the build links the C runtime statically.

Checks every native file in the wheel, not just the first one named speakrs-diar:
a wheel can carry both the real binary and a launcher script that execs it.

Needs lief: uv run --no-project --with lief python scripts/check-speakrs-wheel.py <wheel>
"""

from __future__ import annotations

import os
import sys
import tempfile
import zipfile
from pathlib import Path

import lief

MACHO_ALLOWED_PREFIXES = ("/usr/lib/", "/System/Library/", "@loader_path/", "@rpath/", "@executable_path/")
ELF_ALLOWED = {
    "libc.so.6",
    "libm.so.6",
    "libgcc_s.so.1",
    "libpthread.so.0",
    "libdl.so.2",
    "librt.so.1",
    "ld-linux-x86-64.so.2",
    "ld-linux-aarch64.so.1",
}
# The Visual C++ redistributable, which is not part of Windows.
PE_DENIED_PREFIXES = ("vcruntime", "msvcp", "concrt", "vccorlib", "ucrtbased")


def linked_libraries(path: Path) -> tuple[str, list[str]] | None:
    """(format, linked libraries), or None when the file is not a native binary."""
    name = str(path)
    if lief.is_macho(name):
        binary = lief.parse(name)
        if isinstance(binary, lief.MachO.FatBinary):
            binary = binary.at(0)
        # A dylib's first entry is its own install name, not a dependency.
        libs = [
            lib.name
            for lib in binary.libraries
            if lib.command != lief.MachO.LoadCommand.TYPE.ID_DYLIB
        ]
        return "Mach-O", libs
    if lief.is_elf(name):
        return "ELF", list(lief.parse(name).libraries)
    if lief.is_pe(name):
        return "PE", list(lief.parse(name).libraries)
    return None


def problems(kind: str, libs: list[str]) -> list[str]:
    if kind == "Mach-O":
        return [lib for lib in libs if not lib.startswith(MACHO_ALLOWED_PREFIXES)]
    if kind == "ELF":
        return [lib for lib in libs if lib not in ELF_ALLOWED]
    bad = [lib for lib in libs if lib.lower().startswith(PE_DENIED_PREFIXES)]
    # On a Windows runner, also require each DLL to be one Windows itself ships.
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    if sys.platform == "win32":
        present = {entry.name.lower() for entry in system32.iterdir()}
        bad += [
            lib
            for lib in libs
            if lib not in bad
            and not lib.lower().startswith(("api-ms-win-", "ext-ms-"))
            and lib.lower() not in present
        ]
    return bad


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    wheel = Path(argv[1])
    lief.logging.disable()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(root)

        checked: list[str] = []
        leaks: list[str] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            found = linked_libraries(path)
            if found is None:
                continue
            kind, libs = found
            name = str(path.relative_to(root))
            checked.append(f"{name} ({kind})")
            leaks += [f"{name}: {lib}" for lib in problems(kind, libs)]

        if not checked:
            print(f"error: no native binaries in {wheel.name}; this check would pass vacuously", file=sys.stderr)
            return 1
        if leaks:
            print("error: wheel links libraries that will not exist on the target machine:", file=sys.stderr)
            for leak in leaks:
                print(f"  {leak}", file=sys.stderr)
            return 1

        print(f"{wheel.name}: self-contained ({len(checked)} native files checked)")
        for name in checked:
            print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
