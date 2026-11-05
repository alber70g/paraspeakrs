import sys

import pytest

from paraspeakrs import fileio
from paraspeakrs.fileio import replace_file


class _BusyFile:
    """A temp file whose replace() is refused while a reader holds the target, as
    Windows does."""

    def __init__(self, refusals: int) -> None:
        self.refusals = refusals
        self.calls = 0

    def replace(self, target):
        self.calls += 1
        if self.calls <= self.refusals:
            raise PermissionError(5, "Access is denied")


def test_windows_retries_while_a_reader_holds_the_target(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(fileio.time, "sleep", lambda seconds: None)
    busy = _BusyFile(refusals=3)

    replace_file(busy, "job.json")

    assert busy.calls == 4


def test_windows_gives_up_eventually(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(fileio.time, "sleep", lambda seconds: None)

    with pytest.raises(PermissionError):
        replace_file(_BusyFile(refusals=1000), "job.json")


def test_elsewhere_a_refusal_is_not_retried(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    busy = _BusyFile(refusals=1)

    with pytest.raises(PermissionError):
        replace_file(busy, "job.json")
    assert busy.calls == 1


def test_replaces_a_real_file(tmp_path):
    source, target = tmp_path / "new.json", tmp_path / "job.json"
    source.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")

    replace_file(source, target)

    assert target.read_text(encoding="utf-8") == "new"
    assert not source.exists()
