#!/usr/bin/env python3
"""Fail unless every Mach-O file in a speakrs wheel is self-contained.

The wheel has to run on a machine with no Homebrew, so a reference to
/opt/homebrew or /usr/local is a broken install waiting to happen. maturin's
--auditwheel=repair vendors the gfortran runtime to prevent that; this checks it
actually happened.

Checks every Mach-O in the wheel, not just the first file named speakrs-diar:
with repair enabled the wheel holds *two* such files -- the real binary and a
Python launcher that execs it -- and os.walk yields the launcher first. `otool`
cannot read the launcher, so a check that stopped there passed without testing
anything.

Usage: check-speakrs-wheel.py <wheel>
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

BAD_PREFIXES = ("/opt/homebrew", "/usr/local")


def dependencies(path: Path) -> list[str] | None:
    """Linked dylibs, or None when the file is not Mach-O."""
    result = subprocess.run(["otool", "-L", str(path)], capture_output=True, text=True)
    if result.returncode != 0 or "is not an object file" in result.stdout + result.stderr:
        return None
    return [line.strip() for line in result.stdout.splitlines()[1:] if line.strip()]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    wheel = Path(argv[1])

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(root)

        checked: list[str] = []
        leaks: list[str] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            links = dependencies(path)
            if links is None:
                continue
            name = str(path.relative_to(root))
            checked.append(name)
            leaks += [f"{name}: {link}" for link in links if link.startswith(BAD_PREFIXES)]

        if not checked:
            print(f"error: no Mach-O files in {wheel.name}; this check would pass vacuously", file=sys.stderr)
            return 1
        if leaks:
            print("error: wheel references paths that will not exist on the target machine:", file=sys.stderr)
            for leak in leaks:
                print(f"  {leak}", file=sys.stderr)
            return 1

        print(f"{wheel.name}: self-contained ({len(checked)} Mach-O files checked)")
        for name in checked:
            print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
