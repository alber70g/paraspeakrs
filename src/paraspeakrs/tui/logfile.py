from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(workspace_dir: Path) -> Path:
    """Send log records to ``<workspace>/logs/tui.log`` and return that path.

    The TUI owns the terminal, so anything written to stderr is either painted
    over or lost; a bug report from someone else's machine needs a file to
    attach instead.
    """
    log_path = Path(workspace_dir) / "logs" / "tui.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return log_path
