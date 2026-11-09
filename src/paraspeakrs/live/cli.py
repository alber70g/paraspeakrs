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
    follow.add_argument(
        "--no-speakers",
        action="store_true",
        help="do not tell voices apart: every line is --me or --others, by track",
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
    session_dir = _session_dir(env.workspace_dir, "live")
    speakers = None if args.no_speakers else _live_speakers(env, session_dir)
    source = RecordingSource(
        target,
        asr,
        me=args.me,
        others=args.others,
        speakers=speakers,
        state_root=env.workspace_dir / "live" / "recordings",
    )
    print(f"following recordings in {target}" if target.is_dir() else f"following {target}", file=sys.stderr)
    tracks = f"{args.me} (microphone), {args.others} (system audio)"
    if speakers is None:
        print(f"speakers:  {tracks}", file=sys.stderr)
    else:
        print(f"speakers:  told apart by voice per track: {tracks}; voices from {speakers.book.cache.path}", file=sys.stderr)
    _serve(args, source, session_dir)


def _live_speakers(env: Settings, session_dir: Path):
    """Voices told apart with the speakrs sidecar, or None (with the reason) when it is missing."""
    from ..audio import AudioPreparer
    from ..cache import SpeakerCache, embedding_namespace
    from ..diarization import SpeakrsDiarizer
    from .speakers import SpeakerBook
    from .windows import LiveSpeakers, WindowDiarizer

    if not env.speakrs_bin.exists():
        print(f"speakers:  speakrs-diar not found at {env.speakrs_bin}; lines are named by track", file=sys.stderr)
        return None
    if env.speakrs_mode == "cpu":
        # Measured: a 30 s window took 26 s on CPU against 0.8 s on CoreML.
        print("speakers:  SPEAKRS_MODE=cpu is too slow to keep up live; try --no-speakers", file=sys.stderr)
    diarizer = SpeakrsDiarizer(
        AudioPreparer(), binary=env.speakrs_bin, models_dir=env.speakrs_models_dir, mode=env.speakrs_mode
    )
    cache = SpeakerCache(env.workspace_dir / "speaker-cache.json", namespace=embedding_namespace("speakrs"))
    # Kept with the recording once it is found (`SpeakerBook.attach`), so a restart picks it up.
    book = SpeakerBook(cache, session_id=session_dir.name)
    return LiveSpeakers(book, WindowDiarizer(diarizer, session_dir / "windows"))


def replay(args: argparse.Namespace, workspace_dir: Path) -> None:
    from .server import ReplaySource
    from .transcript import load_lines

    lines = load_lines(args.transcript)
    print(f"{len(lines)} lines from {args.transcript.name}, replayed at {args.speed:g}x", file=sys.stderr)
    source = ReplaySource(args.transcript.name, lines, speed=args.speed)
    _serve(args, source, _session_dir(workspace_dir, args.transcript.stem))


def _session_dir(workspace_dir: Path, name: str) -> Path:
    from .brief import slug

    stamp = time.strftime("%Y%m%d-%H%M%S")
    return workspace_dir / "live" / "sessions" / f"{stamp}-{slug(name) or 'meeting'}"


def _serve(args: argparse.Namespace, source, session_dir: Path) -> None:
    import uvicorn

    from .analyzer import Thresholds
    from .brief import load_brief
    from .server import LiveSession, create_app

    brief = load_brief(args.brief) if args.brief else None
    decisions, chat = _clients(args)
    session = LiveSession(
        source,
        decisions,
        chat,
        session_dir=session_dir,
        # <workspace>/live/briefs, next to <workspace>/live/sessions.
        briefs_dir=session_dir.parents[1] / "briefs",
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
