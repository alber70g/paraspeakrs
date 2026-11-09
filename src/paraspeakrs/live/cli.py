"""`paraspeakrs live`: follow a recording or replay a transcript with live analysis, or check the models."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from ..config import Settings

from .chat import DEFAULT_CHAT_URL, ChatClient, ChatError
from .decisions import DEFAULT_DECISIONS_MODEL, DEFAULT_DECISIONS_URL, KNOWN_PATHS, DecisionClient, DecisionError, noul


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    from .listen import DEFAULT_RECORDINGS_DIR

    live = subparsers.add_parser(
        "live", help="live meeting analysis (spike): personas, a JEV decision model and a chat model"
    )
    commands = live.add_subparsers(dest="live_command", required=True)

    follow = commands.add_parser(
        "follow", help="transcribe the meeting the recorder is recording, and analyze it live in a web page"
    )
    follow.add_argument(
        "recording",
        nargs="?",
        type=Path,
        default=DEFAULT_RECORDINGS_DIR,
        help="a recordings folder, to follow the recording in progress there or the next one to start "
        "(default: %(default)s), or one recording's .mic.caf",
    )
    follow.add_argument("--me", default="Me", help="speaker name for the microphone track (default: Me)")
    follow.add_argument(
        "--others", default="Others", help="speaker name for the system-audio track (default: Others)"
    )
    _add_session_flags(follow)

    replay = commands.add_parser("replay", help="play a finished transcript back as if live, in a web page")
    replay.add_argument("transcript", type=Path, help="a paraspeakrs transcript: the .txt, or the JSON of `run`")
    replay.add_argument(
        "--speed", type=float, default=10.0, help="times real time; 0 replays without waiting (default 10)"
    )
    _add_session_flags(replay)

    check = commands.add_parser("check", help="check that the decision model and the chat model answer")
    _add_model_flags(check)


def _add_session_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--brief", type=Path, default=None, help="a brief (JSON) to use instead of the questionnaire")
    parser.add_argument("--step", type=float, default=30.0, help="seconds of meeting per analysis step (default 30)")
    parser.add_argument(
        "--topic-threshold", type=float, default=0.6, help="p(topic changed) that starts a new section (default 0.6)"
    )
    parser.add_argument(
        "--comment-threshold", type=float, default=0.5, help="p(comment) that makes a persona speak (default 0.5)"
    )
    parser.add_argument(
        "--min-section", type=float, default=60.0, help="seconds before a topic change can close a section (default 60)"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="do not open the page")
    _add_model_flags(parser)


def _add_model_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--decisions-url",
        default=os.getenv("PARASPEAKRS_DECISIONS_URL", DEFAULT_DECISIONS_URL),
        help="JEV Decisions API endpoint (env PARASPEAKRS_DECISIONS_URL, default %(default)s)",
    )
    parser.add_argument(
        "--decisions-model",
        default=os.getenv("PARASPEAKRS_DECISIONS_MODEL", DEFAULT_DECISIONS_MODEL),
        help="model sent to the decision endpoint (env PARASPEAKRS_DECISIONS_MODEL, default %(default)s)",
    )
    parser.add_argument(
        "--chat-url",
        default=os.getenv("PARASPEAKRS_CHAT_URL", DEFAULT_CHAT_URL),
        help="OpenAI-compatible base URL (env PARASPEAKRS_CHAT_URL, default %(default)s)",
    )
    parser.add_argument(
        "--chat-model",
        default=os.getenv("PARASPEAKRS_CHAT_MODEL"),
        help="chat model (env PARASPEAKRS_CHAT_MODEL; default: the first one the server lists)",
    )


def _clients(args: argparse.Namespace) -> tuple[DecisionClient, ChatClient]:
    # Keys come from the environment only, so they never land in shell history.
    decisions = DecisionClient(
        args.decisions_url, args.decisions_model, api_key=os.getenv("PARASPEAKRS_DECISIONS_API_KEY")
    )
    chat = ChatClient(args.chat_url, args.chat_model, api_key=os.getenv("PARASPEAKRS_CHAT_API_KEY"))
    return decisions, chat


def run(args: argparse.Namespace, env: Settings) -> None:
    if args.live_command == "check":
        if not asyncio.run(check(args)):
            raise SystemExit(1)
        return
    if args.live_command == "follow":
        follow(args, env)
        return
    replay(args, env.workspace_dir)


def follow(args: argparse.Namespace, env: Settings) -> None:
    from ..dictate import build_asr
    from .listen import RecordingSource

    # Fails here, with what to run, when the model is not on disk: a meeting must
    # not wait for a 2.4 GB download.
    asr = build_asr(env)
    target = args.recording.expanduser()
    source = RecordingSource(target, asr, me=args.me, others=args.others)
    print(f"following recordings in {target}" if target.is_dir() else f"following {target}", file=sys.stderr)
    print(f"speakers:  {args.me} (microphone), {args.others} (system audio)", file=sys.stderr)
    _serve(args, source, env.workspace_dir, "live")


def replay(args: argparse.Namespace, workspace_dir: Path) -> None:
    from .server import ReplaySource
    from .transcript import load_lines

    lines = load_lines(args.transcript)
    print(f"{len(lines)} lines from {args.transcript.name}, replayed at {args.speed:g}x", file=sys.stderr)
    _serve(args, ReplaySource(args.transcript.name, lines, speed=args.speed), workspace_dir, args.transcript.stem)


def _serve(args: argparse.Namespace, source, workspace_dir: Path, name: str) -> None:
    import uvicorn

    from .analyzer import Thresholds
    from .brief import load_brief, slug
    from .server import LiveSession, create_app

    brief = load_brief(args.brief) if args.brief else None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    session_dir = workspace_dir / "live" / "sessions" / f"{stamp}-{slug(name) or 'meeting'}"
    decisions, chat = _clients(args)
    session = LiveSession(
        source,
        decisions,
        chat,
        session_dir=session_dir,
        briefs_dir=workspace_dir / "live" / "briefs",
        thresholds=Thresholds(
            topic=args.topic_threshold, comment=args.comment_threshold, min_section_seconds=args.min_section
        ),
        brief=brief,
        step=args.step,
    )
    url = f"http://{args.host}:{args.port}/"
    print(f"decisions: {decisions.url} ({decisions.model})", file=sys.stderr)
    print(f"chat:      {chat.base_url} ({chat.model or 'first listed'})", file=sys.stderr)
    print(f"events:    {session_dir / 'events.jsonl'}", file=sys.stderr)
    print(f"open {url}", file=sys.stderr)
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, (url,)).start()
    uvicorn.run(create_app(session), host=args.host, port=args.port, log_level="warning")


SAMPLE_STATE = "[00:00:05] Anna: Then we move the launch to the third of March.\n[00:00:09] Ben: Agreed."
SAMPLE_QUESTION = {"decided": noul("Does `state` record a decision about a date?")}


async def check(args: argparse.Namespace) -> bool:
    """Call each model once and say what works, and where the decision endpoint is if not where configured."""
    decisions, chat = _clients(args)
    ok = True
    try:
        model = await chat.model_id()
        reply = await chat.complete([{"role": "user", "content": "Reply with the single word OK."}], max_tokens=512)
        print(f"chat       ok    {chat.base_url}  model {model}  replied {reply[:40]!r}")
    except ChatError as exc:
        ok = False
        print(f"chat       FAIL  {exc}")
    try:
        answers = await decisions.decide(SAMPLE_STATE, SAMPLE_QUESTION)
        p = answers["decided"].p_yes  # type: ignore[union-attr]
        print(f"decisions  ok    {decisions.url}  model {decisions.model}  p(yes)={p:.2f} on a question whose answer is yes")
    except DecisionError as exc:
        ok = False
        print(f"decisions  FAIL  {exc}")
        if " answered 404" in str(exc):
            found = await _probe(decisions)
            if found:
                print(f"           the API answers at {found}: pass --decisions-url {found}")
    finally:
        await decisions.aclose()
        await chat.aclose()
    return ok


async def _probe(decisions: DecisionClient) -> str | None:
    parts = urlsplit(decisions.url)
    base = f"{parts.scheme}://{parts.netloc}"
    for path in KNOWN_PATHS:
        url = base + path
        if url == decisions.url:
            continue
        candidate = DecisionClient(url, decisions.model, client=decisions._client)
        try:
            await candidate.decide(SAMPLE_STATE, SAMPLE_QUESTION)
        except DecisionError:
            continue
        return url
    return None
