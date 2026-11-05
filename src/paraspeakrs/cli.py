from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from . import __version__
from .config import Settings, default_sherpa_model_dir
from .factory import build_pipeline
from .labeling import LabelingWorkspace
from .mcp_store import ArtifactStore
from .models import LabelingArtifacts, PipelineArtifacts

JSON_HELP = "print the full report as JSON instead of a short summary"


def main() -> None:
    """Entry point. Expected failures exit 2 with a message, not a traceback.

    A missing model or a blocked download is a normal thing to hit on a first run,
    and the message already says what to do about it; a stack trace only buries it.
    """
    try:
        _main()
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _main() -> None:
    # `mcp` forwards its flags verbatim to the MCP server, which owns their
    # definitions. That handoff happens before argparse sees the command line:
    # argparse.REMAINDER does not capture a *leading* option, so `mcp --transport
    # stdio` was rejected as an unrecognized argument of this parser instead.
    argv = sys.argv[1:]
    if argv and argv[0] == "mcp":
        from .mcp_server import main as mcp_main

        mcp_main(argv[1:])
        return

    parser = argparse.ArgumentParser(
        prog="paraspeakrs",
        description="Speaker-aware transcription for long meeting recordings.",
    )
    parser.add_argument("--version", action="version", version=f"paraspeakrs {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    env = Settings.from_env()
    tui_parser = subparsers.add_parser("tui", help="launch the terminal UI (needs the 'tui' extra)")
    tui_parser.add_argument(
        "start_dir",
        nargs="?",
        type=Path,
        default=None,
        help="directory the file browser opens in (default: where you left it last time)",
    )
    subparsers.add_parser("mcp", help="run the MCP server (needs the 'mcp' extra); see `paraspeakrs mcp --help`")
    fetch_parser = subparsers.add_parser("fetch-models", help="download the ASR model ahead of first use")
    fetch_parser.add_argument(
        "--sherpa-model-dir",
        type=Path,
        default=None,
        help=f"where to put the model (default: {env.sherpa_model_dir})",
    )
    fetch_parser.add_argument(
        "--sherpa-precision",
        choices=["fp32", "fp16", "int8"],
        default=None,
        help="fp32 (2.4 GB) and fp16 (1.2 GB) transcribe far better than int8 (490 MB). "
        "Omit to be asked, with a recommendation for this machine.",
    )
    recorder_parser = subparsers.add_parser(
        "install-recorder",
        help="build the macOS meeting recorder (mic + system audio) and install it in ~/Applications",
    )
    recorder_parser.add_argument("--dest", type=Path, default=None, help="install directory (default: ~/Applications)")
    recorder_parser.add_argument(
        "--sign",
        default=None,
        metavar="IDENTITY",
        help="codesign identity (default: a Developer ID or Apple Development certificate if present, else ad hoc)",
    )
    recorder_parser.add_argument("--no-open", action="store_true", help="install without launching the app")
    record_parser = subparsers.add_parser(
        "record",
        help="Linux/Windows: record a meeting (microphone + system audio), then transcribe it",
    )
    _add_common(record_parser, env)
    record_parser.add_argument(
        "--out-dir", type=Path, default=None, help="where recordings go (default: ~/Documents/paraspeakrs-recordings)"
    )
    record_parser.add_argument("--seconds", type=float, default=None, help="stop after this many seconds")
    record_parser.add_argument("--mic", default=None, metavar="NAME", help="input device whose name contains NAME")
    record_parser.add_argument(
        "--system", default=None, metavar="NAME", help="device to record system audio from (see --list-devices)"
    )
    record_parser.add_argument("--list-devices", action="store_true", help="list the audio devices and exit")
    record_parser.add_argument("--no-transcribe", action="store_true", help="save the recording only")
    dictate_parser = subparsers.add_parser(
        "dictate", help="print the text of one short 16-bit WAV (the recorder's dictation shortcut)"
    )
    dictate_parser.add_argument("audio_path", nargs="?", type=Path, default=None)
    dictate_parser.add_argument(
        "--serve",
        action="store_true",
        help='load the model, print {"ready": true}, then answer one JSON line per WAV path read from stdin',
    )
    serve_parser = subparsers.add_parser("serve")
    _add_common(serve_parser, env)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    run_parser = subparsers.add_parser("run")
    _add_common(run_parser, env)
    run_parser.add_argument("audio_path", type=Path)
    run_parser.add_argument("--txt", action="store_true", help="print the formatted transcript instead of JSON")
    run_parser.add_argument("--force", action="store_true", help="re-transcribe even if this audio was already done")
    run_parser.add_argument(
        "--single-speaker-channel",
        action="append",
        choices=["L", "R"],
        default=[],
        help="stereo channel that carries one person (e.g. your own mic); repeatable",
    )
    label_parser = subparsers.add_parser("label-dir")
    _add_common(label_parser, env)
    label_parser.add_argument("audio_dir", type=Path)
    label_parser.add_argument("--label-probe-seconds", type=float, default=300.0)
    label_parser.add_argument("--transcribe-after-label", action="store_true")
    agent_parser = subparsers.add_parser(
        "agent", help="file-based transcribe-and-name flow for coding agents; see `paraspeakrs agent help`"
    )
    agent_commands = agent_parser.add_subparsers(dest="agent_command", required=True)
    agent_commands.add_parser("help", help="print the guide for driving this flow")
    agent_transcribe = agent_commands.add_parser("transcribe", help="transcribe into a directory of speaker samples")
    _add_common(agent_transcribe, env)
    agent_transcribe.add_argument("audio_path", type=Path)
    agent_transcribe.add_argument("-o", "--out-dir", type=Path, required=True)
    agent_transcribe.add_argument("--json", action="store_true", help=JSON_HELP)
    agent_diarize = agent_commands.add_parser(
        "diarize", help="diarize only (no ASR) into a directory of speaker samples"
    )
    _add_common(agent_diarize, env)
    agent_diarize.add_argument("audio_path", type=Path)
    agent_diarize.add_argument("-o", "--out-dir", type=Path, required=True)
    agent_diarize.add_argument("--json", action="store_true", help=JSON_HELP)
    agent_apply = agent_commands.add_parser("apply", help="apply the names given by renaming the samples")
    _add_common(agent_apply, env)
    agent_apply.add_argument("out_dir", type=Path)
    agent_apply.add_argument("--dry-run", action="store_true", help="report what would change, change nothing")
    agent_apply.add_argument("--json", action="store_true", help=JSON_HELP)
    args = parser.parse_args()

    if args.command == "agent" and args.agent_command == "help":
        from importlib.resources import files

        print(files("paraspeakrs").joinpath("agent_guide.md").read_text(encoding="utf-8"), end="")
        return

    # These do not take the pipeline flags, so they are dispatched before
    # _settings_from_args goes looking for them.
    if args.command == "tui":
        from .tui import main as tui_main

        tui_main(args.start_dir)
        return
    if args.command == "install-recorder":
        from . import recorder_install

        try:
            recorder_install.install(
                dest_dir=args.dest or recorder_install.DEFAULT_DEST,
                identity=args.sign,
                open_app=not args.no_open,
            )
        except recorder_install.CommandFailed as exc:
            raise RuntimeError(
                f"{exc}\n\nReport it on GitHub; the link fills in the title and this log. "
                f"Check the text before you submit:\n{recorder_install.issue_url(str(exc))}"
            ) from exc
        return
    if args.command == "dictate":
        from . import dictate

        dictate.main(args.audio_path, serve_mode=args.serve)
        return
    if args.command == "fetch-models":
        from .model_fetch import ensure_sherpa_model, model_is_present

        from . import precision as precision_mod

        chosen = args.sherpa_precision or precision_mod.resolve(
            workspace_dir=env.workspace_dir,
            model_root=default_sherpa_model_dir(env.sherpa_precision, env.workspace_dir).parent,
            requested=env.sherpa_precision,
            explicit=env.precision_is_explicit,
        )
        model_dir = args.sherpa_model_dir or default_sherpa_model_dir(chosen, env.workspace_dir)
        if model_is_present(model_dir):
            print(f"ASR model already present at {model_dir}", file=sys.stderr)
            return
        ensure_sherpa_model(model_dir, chosen)
        return

    settings = _settings_from_args(args, env)

    if args.command == "serve":
        import uvicorn

        from .api import create_app

        app = create_app(settings)
        uvicorn.run(app, host=args.host, port=args.port)
    elif args.command == "run":
        # Go through the same store the TUI and MCP server use, so a CLI run is
        # listed as a job, can be labeled later, and is not redone next time.
        workspace = LabelingWorkspace(
            build_pipeline(settings),
            ArtifactStore(settings.workspace_dir / "mcp-jobs"),
        )
        summary = workspace.transcribe(
            args.audio_path,
            force=args.force,
            single_speaker_channels=frozenset(args.single_speaker_channel),
        )
        if summary.reused:
            print(f"reusing job {summary.job_id} for identical audio", file=sys.stderr)
        if args.txt:
            print(workspace.transcript_text(summary.job_id), end="")
        else:
            print(workspace.store.load(summary.job_id).result.model_dump_json(indent=2))
    elif args.command == "record":
        _record(args, settings)
    elif args.command == "agent":
        from . import agent_dir, agent_text

        # Only `transcribe` runs ASR; `diarize` must not load, download or ask
        # about the model, and `apply` never transcribes either way.
        workspace = LabelingWorkspace(
            build_pipeline(settings, need_asr=args.agent_command == "transcribe"),
            ArtifactStore(settings.workspace_dir / "mcp-jobs"),
        )
        if args.agent_command == "transcribe":
            report = agent_dir.prepare(workspace, args.audio_path, args.out_dir)
        elif args.agent_command == "diarize":
            report = agent_dir.diarize(workspace, args.audio_path, args.out_dir)
        else:
            report = agent_dir.apply(workspace, args.out_dir, dry_run=args.dry_run)
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            summarize = agent_text.applied if args.agent_command == "apply" else agent_text.staged
            print(summarize(report), end="")
    elif args.command == "label-dir":
        _label_dir(
            args.audio_dir,
            settings,
            label_probe_seconds=args.label_probe_seconds,
            transcribe_after_label=args.transcribe_after_label,
        )


def _record(args: argparse.Namespace, settings: Settings) -> None:
    """Record, merge, then transcribe the way the macOS app does: your mic on
    the left channel is one speaker, everyone else is diarized on the right."""
    import subprocess

    from . import record as record_mod
    from .config import RECORDER_BIN_NAME, default_recorder_bin

    binary = default_recorder_bin()
    if not binary.is_file():
        if sys.platform == "darwin":
            raise RuntimeError(
                "on macOS, meetings are recorded by the menu-bar app: paraspeakrs install-recorder"
            )
        raise RuntimeError(
            f"{RECORDER_BIN_NAME} not found: {binary}\n"
            "It ships in the paraspeakrs-speakrs wheel for Linux x86_64 and Windows x64. "
            "From a checkout:\n"
            "  cd packages/speakrs-diar && cargo build --release --features recorder"
        )
    if args.list_devices:
        raise SystemExit(subprocess.run([str(binary), "--list-devices"]).returncode)

    recording = record_mod.record(
        [str(binary)],
        args.out_dir or record_mod.RECORDINGS_DIR,
        seconds=args.seconds,
        mic=args.mic,
        system=args.system,
    )
    print(f"saved {recording.path} ({recording.seconds:.0f} s)", file=sys.stderr)
    if args.no_transcribe:
        return

    later = f"paraspeakrs run --single-speaker-channel L --txt {recording.path}"
    try:
        workspace = LabelingWorkspace(
            build_pipeline(settings),
            ArtifactStore(settings.workspace_dir / "mcp-jobs"),
        )
        summary = workspace.transcribe(recording.path, single_speaker_channels=frozenset({"L"}))
        transcript = recording.path.with_suffix(".txt")
        transcript.write_text(workspace.transcript_text(summary.job_id), encoding="utf-8")
    except (RuntimeError, ValueError) as exc:
        raise RuntimeError(f"{exc}\nThe recording is saved; transcribe it later with:\n  {later}") from exc
    print(f"transcript: {transcript}  (name the speakers in `paraspeakrs tui`)", file=sys.stderr)


def _add_common(parser: argparse.ArgumentParser, env: Settings) -> None:
    """Flags default to the environment, so a flag overrides an env var.

    Without this the CLI ignored PARAKEET_* entirely while the TUI and MCP
    server honoured them, and the two ended up reading different job stores.
    """
    parser.add_argument("--device", choices=["cpu", "cuda", "coreml"], default=env.device)
    parser.add_argument("--workspace-dir", type=Path, default=env.workspace_dir)
    parser.add_argument("--diar-backend", choices=["senko", "speakrs"], default=env.diar_backend)
    parser.add_argument("--senko-device", choices=["auto", "cpu", "cuda", "coreml"], default=env.senko_device)
    parser.add_argument("--senko-no-warmup", action="store_true")
    parser.add_argument("--speakrs-bin", type=Path, default=env.speakrs_bin)
    parser.add_argument("--speakrs-models-dir", type=Path, default=env.speakrs_models_dir)
    parser.add_argument(
        "--speakrs-mode",
        choices=["cpu", "coreml", "coreml-fast", "cuda", "cuda-fast"],
        default=env.speakrs_mode,
    )
    parser.add_argument("--asr-backend", choices=["sherpa", "openai", "mock"], default=env.asr_backend)
    parser.add_argument("--sherpa-model-dir", type=Path, default=env.sherpa_model_dir)
    parser.add_argument(
        "--sherpa-precision",
        choices=["fp32", "fp16", "int8"],
        default=None,
        help="ASR weights (fp32: 2.4 GB, best; fp16: 1.2 GB, same transcript; int8: 490 MB, drops speech). "
        "Omitted on a first run, paraspeakrs asks.",
    )
    parser.add_argument("--sherpa-num-threads", type=int, default=env.sherpa_num_threads)
    parser.add_argument("--openai-base-url", default=env.openai_base_url)
    parser.add_argument("--openai-api-key", default=env.openai_api_key)
    parser.add_argument("--openai-model", default=env.openai_model)


def _model_dir_for(args: argparse.Namespace, env: Settings) -> Path:
    """Keep --sherpa-precision and --sherpa-model-dir from contradicting each other.

    The model-dir default is derived from the *environment's* precision, so asking for
    a different precision on the command line would otherwise download those weights
    into the other precision's directory. The same goes for --workspace-dir, which
    owns the models directory. An explicit --sherpa-model-dir still wins.
    """
    if args.sherpa_model_dir != env.sherpa_model_dir:
        return args.sherpa_model_dir
    precision = args.sherpa_precision or env.sherpa_precision
    if precision != env.sherpa_precision or args.workspace_dir != env.workspace_dir:
        return default_sherpa_model_dir(precision, args.workspace_dir.expanduser().resolve())
    return args.sherpa_model_dir


def _settings_from_args(args: argparse.Namespace, env: Settings) -> Settings:
    return Settings(
        device=args.device,
        workspace_dir=args.workspace_dir,
        diar_backend=args.diar_backend,
        senko_device=args.senko_device,
        senko_warmup=env.senko_warmup and not args.senko_no_warmup,
        speakrs_bin=args.speakrs_bin,
        speakrs_models_dir=args.speakrs_models_dir,
        speakrs_mode=args.speakrs_mode,
        asr_backend=args.asr_backend,
        sherpa_model_dir=_model_dir_for(args, env),
        sherpa_precision=args.sherpa_precision or env.sherpa_precision,
        precision_is_explicit=args.sherpa_precision is not None or env.precision_is_explicit,
        sherpa_num_threads=args.sherpa_num_threads,
        openai_base_url=args.openai_base_url,
        openai_api_key=args.openai_api_key,
        openai_model=args.openai_model,
    )


def _label_dir(
    audio_dir: Path,
    settings: Settings,
    label_probe_seconds: float | None,
    transcribe_after_label: bool,
) -> None:
    pipeline = build_pipeline(settings)
    audio_files = sorted(
        (path for path in audio_dir.iterdir() if path.is_file() and not path.name.startswith(".")),
        key=lambda path: path.stat().st_size,
    )
    for audio_path in audio_files:
        print(f"\n=== {audio_path.name} ===")
        work_dir = settings.workspace_dir / "label-runs" / audio_path.stem
        labeling = pipeline.prepare_labeling(
            work_dir=work_dir,
            file_path=audio_path,
            probe_seconds=label_probe_seconds,
        )
        _print_labeling_summary(labeling)
        labels = _prompt_labeling_labels(labeling)
        pipeline.speaker_cache.record_embeddings(labels, labeling.speaker_embeddings)
        labels_path = work_dir / "labels.json"
        labels_path.write_text(labeling.model_dump_json(indent=2), encoding="utf-8")
        print(f"Saved labels/snippets metadata: {labels_path}")
        if not transcribe_after_label:
            continue

        artifacts = pipeline.run_with_artifacts(
            work_dir=settings.workspace_dir / "label-runs" / audio_path.stem,
            file_path=audio_path,
        )
        result_path = settings.workspace_dir / "label-runs" / audio_path.stem / "result.json"
        result_path.write_text(artifacts.result.model_dump_json(indent=2), encoding="utf-8")
        print(f"Saved result: {result_path}")


def _print_labeling_summary(artifacts: LabelingArtifacts) -> None:
    durations: dict[str, float] = defaultdict(float)
    for segment in artifacts.diarization:
        durations[segment.speaker] += max(0.0, segment.end - segment.start)

    for speaker in sorted(durations):
        resolved = artifacts.resolved_labels.get(speaker)
        has_embedding = "yes" if speaker in artifacts.speaker_embeddings else "no"
        print(f"\n{speaker} | resolved={resolved or '-'} | seconds={durations[speaker]:.1f} | embedding={has_embedding}")
        paths = artifacts.snippet_paths.get(speaker, [])
        if paths:
            print("Snippets:")
            for path in paths:
                print(f"  {path}")
        else:
            print("Snippets: none")


def _print_speaker_summary(artifacts: PipelineArtifacts) -> None:
    by_speaker: dict[str, list[str]] = defaultdict(list)
    durations: dict[str, float] = defaultdict(float)
    for segment in artifacts.result.segments:
        by_speaker[segment.speaker].append(segment.text)
        durations[segment.speaker] += max(0.0, segment.end - segment.start)

    for speaker in sorted(by_speaker):
        resolved = next(
            (segment.resolved_label for segment in artifacts.result.segments if segment.speaker == speaker),
            None,
        )
        snippets = " ".join(by_speaker[speaker])[:500].replace("\n", " ")
        has_embedding = "yes" if speaker in artifacts.speaker_embeddings else "no"
        print(f"\n{speaker} | resolved={resolved or '-'} | seconds={durations[speaker]:.1f} | embedding={has_embedding}")
        print(snippets)


def _prompt_labels(artifacts: PipelineArtifacts) -> dict[str, str]:
    labels: dict[str, str] = {}
    speaker_ids = sorted({segment.speaker for segment in artifacts.result.segments})
    for speaker in speaker_ids:
        current = next((segment.resolved_label for segment in artifacts.result.segments if segment.speaker == speaker), None)
        prompt = f"Label for {speaker}"
        if current:
            prompt += f" [{current}]"
        prompt += " (blank to skip/keep): "
        label = input(prompt).strip()
        if label:
            labels[speaker] = label
        elif current:
            labels[speaker] = current
    return labels


def _prompt_labeling_labels(artifacts: LabelingArtifacts) -> dict[str, str]:
    labels: dict[str, str] = {}
    speaker_ids = sorted({segment.speaker for segment in artifacts.diarization})
    for speaker in speaker_ids:
        current = artifacts.resolved_labels.get(speaker)
        prompt = f"Label for {speaker}"
        if current:
            prompt += f" [{current}]"
        prompt += " (blank to skip/keep): "
        label = input(prompt).strip()
        if label:
            labels[speaker] = label
        elif current:
            labels[speaker] = current
    return labels


if __name__ == "__main__":
    main()
