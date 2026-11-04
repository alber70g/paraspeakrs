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


def test_the_dictation_sounds_ship_with_the_swift_sources():
    """The installer copies them into the app bundle and fails without them."""
    sounds = recorder_install.find_sources() / "Sources" / "ParaspeakrsRecorder" / "Sounds"
    assert {path.name for path in sounds.iterdir()} >= {"dictation-start.wav", "dictation-stop.wav", "dictation-cancel.wav"}


def test_a_failed_build_shows_the_compiler_errors_not_only_build_failed():
    """`swift build` prints the errors on stdout and only "Build failed" on stderr;
    without the errors a user on another toolchain cannot say what broke."""
    script = (
        "import sys\n"
        "print('Compiling RecorderCore')\n"
        "print('/Users/a b/PermissionsSection.swift:9:5: error: cannot find type Foo in scope')\n"
        "sys.stderr.write('error: Build failed\\n')\n"
        "sys.exit(1)\n"
    )
    with pytest.raises(RuntimeError) as failure:
        recorder_install._run([recorder_install.sys.executable, "-c", script])

    assert "PermissionsSection.swift:9:5: error: cannot find type Foo in scope" in str(failure.value)
    assert "Compiling RecorderCore" not in str(failure.value)


def test_the_swift_sources_use_no_swiftui_macros_the_command_line_tools_cannot_expand():
    """With the macOS 27.0 SDK, `@State` is a macro whose plugin ships only with Xcode;
    `install-recorder` builds with the Command Line Tools, so the build fails there."""
    import re

    sources = recorder_install.find_sources() / "Sources"
    offenders = [
        f"{path.name}:{number}"
        for path in sources.rglob("*.swift")
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if not line.lstrip().startswith("//") and re.search(r"@State\b", line)
    ]
    assert offenders == []


def test_a_failed_command_offers_a_github_issue_with_the_log_and_toolchain(monkeypatch):
    """A build failure on someone else's toolchain cannot be fixed without its log
    and the Swift and SDK versions; the link carries them so the report is complete."""
    from urllib.parse import parse_qs, urlparse

    home = str(recorder_install.Path.home())
    failure = recorder_install.CommandFailed(f"swift failed:\n  {home}/x/PermissionsSection.swift:8:24: error: no macro")
    monkeypatch.setattr(recorder_install, "install", lambda **kwargs: (_ for _ in ()).throw(failure))
    monkeypatch.setattr(recorder_install, "_toolchain", lambda: {"Swift": "Apple Swift version 6.4", "SDK": "27.0"})
    monkeypatch.setattr(cli.sys, "argv", ["paraspeakrs", "install-recorder", "--no-open"])

    with pytest.raises(RuntimeError) as raised:
        cli._main()

    url = str(raised.value).split()[-1]
    assert url.startswith("https://github.com/alber70g/paraspeakrs/issues/new?")
    query = parse_qs(urlparse(url).query)
    assert "install-recorder" in query["title"][0]
    body = query["body"][0]
    assert "~/x/PermissionsSection.swift:8:24: error: no macro" in body
    assert "Apple Swift version 6.4" in body and "27.0" in body
    # The issue is public: the user's home directory, and so their username, stays out.
    assert home not in body


def test_a_missing_prerequisite_gets_no_issue_link(monkeypatch):
    """"Install the Command Line Tools" is the fix, not a bug to report."""
    monkeypatch.setattr(recorder_install, "install",
                        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Swift is not installed.")))
    monkeypatch.setattr(cli.sys, "argv", ["paraspeakrs", "install-recorder", "--no-open"])

    with pytest.raises(RuntimeError) as raised:
        cli._main()

    assert "github.com" not in str(raised.value)
