"""`paraspeakrs install-recorder`: what it signs with, and failing before it builds."""

from __future__ import annotations

import pytest

from paraspeakrs import cli, recorder_install

LISTING = """
  1) 1111111111111111111111111111111111111111 "Apple Development: someone@example.com (AAAAAAAAAA)"
  2) 2222222222222222222222222222222222222222 "Developer ID Application: Someone (BBBBBBBBBB)"
     2 valid identities found
"""


def test_developer_id_wins_because_only_it_passes_gatekeeper_elsewhere():
    assert recorder_install.pick_identity(LISTING) == "Developer ID Application: Someone (BBBBBBBBBB)"


def test_apple_development_is_used_when_it_is_the_only_certificate():
    """A stable identity keeps the mic and system-audio grants across rebuilds."""
    listing = LISTING.splitlines()[1] + "\n     1 valid identities found\n"
    assert recorder_install.pick_identity(listing) == "Apple Development: someone@example.com (AAAAAAAAAA)"


def test_no_certificate_falls_back_to_ad_hoc_so_a_fresh_mac_can_still_build():
    assert recorder_install.pick_identity("     0 valid identities found\n") == "-"


def test_missing_swift_fails_with_the_install_command_before_building(monkeypatch):
    """Without the Command Line Tools the message is the fix, not a build log."""
    monkeypatch.setattr(recorder_install.sys, "platform", "darwin")
    monkeypatch.setattr(recorder_install.platform, "mac_ver", lambda: ("26.5", ("", "", ""), "arm64"))
    monkeypatch.setattr(recorder_install.shutil, "which", lambda name: None)
    monkeypatch.setattr(recorder_install, "_run", lambda cmd: pytest.fail(f"ran {cmd}"))

    with pytest.raises(RuntimeError, match="xcode-select --install"):
        recorder_install.install(dest_dir=recorder_install.DEFAULT_DEST, identity=None, open_app=False)


def test_the_swift_sources_are_found_in_a_checkout():
    assert (recorder_install.find_sources() / "Sources" / "ParaspeakrsRecorder" / "Info.plist").is_file()


def test_install_recorder_needs_no_pipeline_settings(monkeypatch):
    """It must not build a pipeline, which would load or download models."""
    seen: list[dict] = []
    monkeypatch.setattr(recorder_install, "install", lambda **kwargs: seen.append(kwargs))
    monkeypatch.setattr(cli, "build_pipeline", lambda *a, **k: pytest.fail("pipeline built"))
    monkeypatch.setattr(cli.sys, "argv", ["paraspeakrs", "install-recorder", "--no-open"])

    cli._main()

    assert seen == [{"dest_dir": recorder_install.DEFAULT_DEST, "identity": None, "open_app": False}]
