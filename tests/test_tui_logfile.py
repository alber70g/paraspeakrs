import logging

from paraspeakrs.tui.logfile import configure_logging


def test_a_failed_run_leaves_its_traceback_in_the_workspace_log(tmp_path):
    """Bug reports come from machines we cannot reach, and the TUI owns the
    terminal, so the only record of why a run failed is this file."""
    log_path = configure_logging(tmp_path)
    try:
        try:
            raise RuntimeError("speakrs-diar failed with exit code 101: thread 'main' panicked")
        except RuntimeError:
            logging.getLogger("paraspeakrs.tui.home").exception("transcribing a.wav failed")
    finally:
        for handler in logging.getLogger().handlers[:]:
            if getattr(handler, "baseFilename", None) == str(log_path):
                logging.getLogger().removeHandler(handler)
                handler.close()

    text = log_path.read_text(encoding="utf-8")
    assert log_path.parent.parent == tmp_path
    assert "transcribing a.wav failed" in text
    assert "Traceback" in text and "panicked" in text
