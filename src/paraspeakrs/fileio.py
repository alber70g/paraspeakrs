from __future__ import annotations

import sys
import time
from pathlib import Path

# 20 tries 25 ms apart: long enough to outlast a reader, which holds a JSON file
# only while read_text() runs, short enough that a real failure still surfaces.
_ATTEMPTS = 20
_PAUSE_SECONDS = 0.025


def replace_file(source: Path, target: Path) -> None:
    """Move ``source`` over ``target`` atomically, as ``Path.replace`` does.

    Windows refuses to replace a file that anything has open, and a reader has
    one open for as long as it reads it: the TUI lists jobs on a worker thread
    while the UI saves one. Unix replaces under open readers, so only Windows
    retries.
    """
    if sys.platform != "win32":
        source.replace(target)
        return
    for attempt in range(_ATTEMPTS):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == _ATTEMPTS - 1:
                raise
            time.sleep(_PAUSE_SECONDS)
