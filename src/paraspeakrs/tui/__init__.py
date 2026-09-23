from __future__ import annotations

import sys

from ..config import Settings
from ..factory import build_pipeline, describe_asr
from ..labeling import LabelingWorkspace
from ..mcp_store import ArtifactStore


def main() -> None:
    settings = Settings.from_env()
    try:
        from .app import DiarizeApp
    except ImportError as exc:
        print(f"failed to import tui: {exc}", file=sys.stderr)
        print(
            "the tui extra is required to run the terminal UI — install it with: uv sync --extra tui",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    try:
        pipeline = build_pipeline(settings)
    except Exception as exc:  # noqa: BLE001 - startup diagnostics for the user
        print(f"failed to build pipeline: {exc}", file=sys.stderr)
        if settings.diar_backend == "speakrs":
            print(
                "the speakrs-diar binary is missing — install the paraspeakrs-speakrs "
                "wheel (macOS arm64), or point SPEAKRS_BIN at your own build",
                file=sys.stderr,
            )
        else:
            print(
                "on macOS a Senko warmup crash is a known issue — retry with SENKO_WARMUP=0",
                file=sys.stderr,
            )
        raise SystemExit(1) from exc

    store = ArtifactStore(settings.workspace_dir / "mcp-jobs")
    workspace = LabelingWorkspace(pipeline, store)

    app = DiarizeApp(workspace, settings.workspace_dir / "ui-state.json")
    # The startup line on stderr is gone once the TUI takes the screen; the header stays.
    app.sub_title = f"ASR: {describe_asr(pipeline.asr)}"
    app.run()


if __name__ == "__main__":
    main()
