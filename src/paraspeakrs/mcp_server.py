from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .config import Settings
from .factory import build_pipeline
from .labeling import JobSummary, LabelingWorkspace
from .mcp_store import ArtifactStore


def _roster(summary: JobSummary) -> list[dict]:
    return [asdict(row) for row in summary.speakers]


def _transcribe_response(summary: JobSummary) -> dict:
    rows = _roster(summary)
    return {
        "job_id": summary.job_id,
        "duration_seconds": round(summary.duration_seconds, 1),
        "num_speakers": summary.num_speakers,
        "unlabeled_speakers": [row.speaker for row in summary.speakers if row.unnamed],
        "speakers": rows,
    }


def create_server(
    settings: Settings | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> FastMCP:
    settings = settings or Settings.from_env()
    pipeline = build_pipeline(settings)
    store = ArtifactStore(settings.workspace_dir / "mcp-jobs")
    workspace = LabelingWorkspace(pipeline, store)
    mcp = FastMCP("parakeet-diarize", host=host, port=port)

    @mcp.tool()
    def transcribe(audio_path: str) -> dict:
        """Transcribe and diarize a local audio file.

        Returns the job_id and the speaker roster. Only speakers with an
        assigned_label appear by name in the transcript; a suggested_label is
        the voice cache's guess and must be confirmed with label_speaker before
        it is used. Does NOT return the transcript text — call get_transcript.
        """
        return _transcribe_response(workspace.transcribe(Path(audio_path)))

    @mcp.tool()
    def list_speakers(job_id: str) -> dict:
        """List a job's speakers with their assigned names, suggestions and talk time."""
        return {"job_id": job_id, "speakers": _roster(workspace.job_summary(job_id))}

    @mcp.tool()
    def get_speaker_audio(job_id: str, speaker_id: str) -> dict:
        """Export a short (up to 10 s) sample clip of one speaker.

        Returns the path to a wav file. Play it for the user so they can
        identify who is speaking, then call label_speaker with the name.
        """
        path = workspace.speaker_sample(job_id, speaker_id)
        return {"job_id": job_id, "speaker": speaker_id, "audio_path": str(path)}

    @mcp.tool()
    def label_speaker(job_id: str, speaker_id: str, name: str) -> dict:
        """Assign a human name to a speaker and remember their voice.

        Stores the speaker's embedding under this name so future transcriptions
        recognize them automatically. Returns the refreshed speaker roster.
        """
        rows = workspace.label_speaker(job_id, speaker_id, name)
        return {"job_id": job_id, "speakers": [asdict(row) for row in rows]}

    @mcp.tool()
    def get_note(job_id: str) -> dict:
        """Read the free-text note recorded about this meeting.

        The note is what the recording was about, in the user's own words - the
        one thing neither the diarization nor the transcript says. Empty string
        when nothing has been written.
        """
        return {"job_id": job_id, "note": workspace.job_note(job_id)}

    @mcp.tool()
    def set_note(job_id: str, note: str) -> dict:
        """Record what this meeting was about. Replaces any existing note.

        This overwrites rather than appends, so read the note first if you mean
        to add to one the user wrote. An empty note clears it. The note is
        written into the transcript as YAML frontmatter, and shown beside the
        recording in the terminal UI.
        """
        return {"job_id": job_id, "note": workspace.set_job_note(job_id, note)}

    @mcp.tool()
    def get_transcript(job_id: str, output_path: str) -> dict:
        """Render the transcript as `[HH:MM:SS] NAME: text` lines.

        Writes the transcript to output_path and returns that path plus a line
        count of the spoken lines. The transcript body is intentionally NOT
        returned, to keep large transcripts out of the model's context window.
        A job with a note gets it as YAML frontmatter above the first line.

        CONSUMER GUIDANCE: pipe transcript retrieval to a file, never into the
        model context. output_path is resolved on the SERVER's filesystem; over
        HTTP transport that is the server host, so co-locate the client/server
        or fetch the file out-of-band. Read/stream the file directly instead of
        pulling the text back through the agent.
        """
        path, lines = workspace.write_transcript(job_id, Path(output_path))
        return {
            "job_id": job_id,
            "output_path": str(path),
            "lines": lines,
            "bytes": path.stat().st_size,
        }

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="paraspeakrs mcp", description="paraspeakrs diarization MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default="stdio",
        help="MCP transport. stdio (default) for local clients, streamable-http for remote/network clients.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host for HTTP transports (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8000, help="Bind port for HTTP transports (default 8000).")
    args = parser.parse_args(argv)
    server = create_server(host=args.host, port=args.port)
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
