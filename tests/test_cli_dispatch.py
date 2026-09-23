"""Subcommand dispatch, especially the two that delegate elsewhere."""

from __future__ import annotations

import pytest

from paraspeakrs import cli


def test_mcp_forwards_its_own_flags(monkeypatch):
    """`mcp` flags belong to the MCP server, and must survive the handoff.

    argparse.REMAINDER does not capture a *leading* option, so routing them
    through a subparser made `paraspeakrs mcp --transport stdio` fail with
    "unrecognized arguments: --transport" before it ever reached the server.
    """
    seen: list[list[str]] = []
    monkeypatch.setattr("paraspeakrs.mcp_server.main", lambda argv=None: seen.append(argv))
    monkeypatch.setattr(cli.sys, "argv", ["paraspeakrs", "mcp", "--transport", "streamable-http", "--port", "9000"])

    cli._main()

    assert seen == [["--transport", "streamable-http", "--port", "9000"]]


def test_mcp_with_no_flags_still_dispatches(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr("paraspeakrs.mcp_server.main", lambda argv=None: seen.append(argv))
    monkeypatch.setattr(cli.sys, "argv", ["paraspeakrs", "mcp"])

    cli._main()

    assert seen == [[]]


def test_tui_dispatches(monkeypatch):
    called: list[bool] = []
    monkeypatch.setattr("paraspeakrs.tui.main", lambda: called.append(True))
    monkeypatch.setattr(cli.sys, "argv", ["paraspeakrs", "tui"])

    cli._main()

    assert called == [True]


def test_expected_failures_exit_2_without_a_traceback(monkeypatch, capsys):
    """A blocked download is a normal first-run outcome; the message is the point."""

    def explode() -> None:
        raise RuntimeError("model download blocked")

    monkeypatch.setattr(cli, "_main", explode)

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 2
    assert "model download blocked" in capsys.readouterr().err


def test_version_flag_reports_the_package_version(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "argv", ["paraspeakrs", "--version"])

    with pytest.raises(SystemExit) as excinfo:
        cli._main()

    assert excinfo.value.code == 0
    assert "paraspeakrs" in capsys.readouterr().out


def test_agent_help_prints_the_packaged_guide(monkeypatch, capsys):
    """The guide is read from the installed package, so it matches the installed CLI."""
    monkeypatch.setattr(cli.sys, "argv", ["paraspeakrs", "agent", "help"])

    cli._main()

    out = capsys.readouterr().out
    assert out.startswith("# paraspeakrs agent guide")
    assert "paraspeakrs agent apply" in out


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["agent", "transcribe", "m.m4a", "-o", "out"], ("prepare", "m.m4a", "out")),
        (["agent", "apply", "out", "--dry-run"], ("apply", "out", True)),
    ],
)
def test_agent_commands_reach_the_directory_flow(monkeypatch, capsys, argv, expected):
    import json

    from paraspeakrs import agent_dir

    monkeypatch.setattr(cli, "build_pipeline", lambda settings: None)
    monkeypatch.setattr(agent_dir, "prepare", lambda ws, audio, out: {"call": ["prepare", str(audio), str(out)]})
    monkeypatch.setattr(agent_dir, "apply", lambda ws, out, dry_run: {"call": ["apply", str(out), dry_run]})
    monkeypatch.setattr(cli.sys, "argv", ["paraspeakrs", *argv])

    cli._main()

    # stdout is pure JSON, so an agent can parse it without scraping.
    assert tuple(json.loads(capsys.readouterr().out)["call"]) == expected
