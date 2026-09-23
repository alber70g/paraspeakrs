"""First-run precision selection.

The question must be asked exactly once, to a human who is watching, and never to a
script. Getting that wrong either blocks an unattended run forever or silently picks
a model that drops a quarter of the transcript.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paraspeakrs import precision


def test_plenty_of_disk_gets_the_best_model():
    choice, why = precision.recommend(ram_gb=32, disk_gb=500)
    assert choice == "fp32"
    assert "fastest" in why


def test_a_nearly_full_disk_falls_back_to_int8():
    """INT8 is a compromise, so the advice has to admit what it costs."""
    choice, why = precision.recommend(ram_gb=32, disk_gb=1.0)
    assert choice == "int8"
    assert "miss words" in why


def test_a_modest_disk_gets_fp16():
    choice, _ = precision.recommend(ram_gb=32, disk_gb=5)
    assert choice == "fp16"


def test_low_ram_warns_but_does_not_downgrade():
    """Every precision peaks at 3-4 GB, so low RAM is a warning, not a reason for INT8."""
    choice, why = precision.recommend(ram_gb=4, disk_gb=500)
    assert choice == "fp32"
    assert "4 GB of RAM is tight" in why


def test_unknown_resources_still_decide():
    """A platform that will not report its RAM must not stall the first run."""
    assert precision.recommend(ram_gb=None, disk_gb=None)[0] == "fp32"


def test_the_table_names_every_option_and_the_ram_surprise(tmp_path):
    text = precision.describe(tmp_path)
    for name in ("fp32", "fp16", "int8"):
        assert name in text
    assert "does not save memory" in text


def test_an_explicit_choice_is_never_second_guessed(tmp_path, monkeypatch):
    monkeypatch.setattr(precision, "can_prompt", lambda: True)
    monkeypatch.setattr(precision, "ask", lambda root: pytest.fail("must not ask"))

    assert precision.resolve(tmp_path, tmp_path, "int8", explicit=True) == "int8"


def test_the_question_is_asked_once_and_remembered(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(precision, "can_prompt", lambda: True)
    monkeypatch.setattr(precision, "ask", lambda root: calls.append(root) or "fp16")

    first = precision.resolve(tmp_path, tmp_path, "fp32", explicit=False)
    second = precision.resolve(tmp_path, tmp_path, "fp32", explicit=False)

    assert (first, second) == ("fp16", "fp16")
    assert len(calls) == 1, "a remembered answer must not be asked for again"


def test_a_script_is_never_asked(tmp_path, monkeypatch):
    """No TTY means no human: blocking on input() would hang the run forever."""
    monkeypatch.setattr(precision, "can_prompt", lambda: False)
    monkeypatch.setattr(precision, "ask", lambda root: pytest.fail("must not ask"))

    assert precision.resolve(tmp_path, tmp_path, "fp32", explicit=False) == "fp32"


def test_noninteractive_env_var_silences_the_prompt(monkeypatch):
    monkeypatch.setenv("PARAKEET_NONINTERACTIVE", "1")
    assert precision.can_prompt() is False


def test_a_workspace_it_cannot_write_does_not_break_the_run(tmp_path):
    """Failing to remember is a nuisance; failing the transcription is not acceptable."""
    blocked = tmp_path / "file-not-a-dir"
    blocked.write_text("")

    precision.remember(blocked / "workspace", "fp32")  # must not raise

    assert precision.saved_choice(blocked / "workspace") is None


def test_garbage_in_the_choice_file_is_ignored(tmp_path):
    (tmp_path / precision.CHOICE_FILE).write_text("bfloat16\n")
    assert precision.saved_choice(tmp_path) is None
