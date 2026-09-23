from pathlib import Path

import pytest

pytest.importorskip("textual")

from paraspeakrs.tui.screens import best_match, fuzzy_matches  # noqa: E402


PATHS = [
    Path("/rec/Voice Memos"),
    Path("/rec/standup-2026-03-04.wav"),
    Path("/rec/interview-jan.m4a"),
]


def test_matches_a_subsequence_not_just_a_prefix():
    """Typing the memorable letters of a name is the point - recordings are
    named by date and device, so the distinguishing part is rarely in front."""
    assert fuzzy_matches(PATHS, "stwav") == [Path("/rec/standup-2026-03-04.wav")]


def test_ignores_case():
    assert fuzzy_matches(PATHS, "voice") == [Path("/rec/Voice Memos")]


def test_drops_everything_that_does_not_match():
    assert fuzzy_matches(PATHS, "zzz") == []


def test_keeps_the_given_order_for_several_matches():
    """The tree sorts its own entries; filtering must not reshuffle them."""
    assert fuzzy_matches(PATHS, "n") == PATHS[1:]


def test_cursor_goes_to_the_strongest_match_not_the_first_listed():
    """The tree lists alphabetically, so the entry you typed for can sit below a
    weaker one - landing on it is the whole point of typing."""
    names = ["notes.txt", "standup-2026-03-04.wav"]
    assert best_match(names, "st") == 1


def test_no_cursor_target_when_nothing_matches():
    assert best_match(["notes.txt"], "zzz") is None


def test_directory_progress_counts_only_recordings(tmp_path):
    """The count is what is left to transcribe; a stray notes file or an
    unrelated subfolder is not a recording you forgot."""
    from paraspeakrs.tui.browse import audio_progress

    (tmp_path / "a.wav").write_bytes(b"a")
    (tmp_path / "b.M4A").write_bytes(b"b")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    assert audio_progress(tmp_path, [(tmp_path / "a.wav").resolve()]) == (1, 2)
