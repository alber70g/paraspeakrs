"""File-based speaker naming for coding agents: the directory is the interface.

``prepare`` transcribes, and ``diarize`` diarizes without running ASR, into a
directory with one sample per speaker, named ``<SPEAKER_ID>__NAME-ME.wav``. The
user renames (or deletes) those files, and ``apply`` reads the directory back
into the job and rewrites the transcript (or, for a ``diarize`` directory, the
diarization) to match.

Apply states the whole directory every time rather than diffing, so it can run
again after any further rename. The job in the shared store stays the source of
truth; the manifest only says which job, and what was *suggested* - which is how
an untouched suggestion is told apart from a name the user typed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .labeling import JobSummary, LabelingWorkspace, SpeakerRow
from .transcript_txt import format_time

MANIFEST = "manifest.json"
TRANSCRIPT = "transcript.txt"
TRANSCRIPT_JSON = "transcript.json"
DIARIZATION_JSON = "diarization.json"
NEXT_STEPS = "NEXT_STEPS.md"
UNNAMED = "NAME-ME"
SEP = "__"
HINT_LINES = 3


@dataclass
class Decision:
    speaker: str
    action: str  # named | suggestion | unnamed | dropped
    name: str | None

    @property
    def teach(self) -> bool:
        """Only a name the user typed teaches the voice library; a guess left alone does not."""
        return self.action == "named"


def prepare(workspace: LabelingWorkspace, audio_path: Path, out_dir: Path) -> dict:
    """Transcribe into ``out_dir``: samples, ``transcript.txt``/``transcript.json`` and NEXT_STEPS.md."""
    out_dir = _check_out_dir(out_dir)
    summary = workspace.transcribe(Path(audio_path))
    files, report = _stage(workspace, summary, out_dir, kind="transcript")
    sample_files = {row.speaker: files[row.speaker].name if row.speaker in files else None for row in summary.speakers}
    workspace.write_transcript(summary.job_id, out_dir / TRANSCRIPT)
    workspace.write_transcript_json(summary.job_id, out_dir / TRANSCRIPT_JSON, sample_files)
    report["transcript"] = str(out_dir / TRANSCRIPT)
    return report


def diarize(workspace: LabelingWorkspace, audio_path: Path, out_dir: Path) -> dict:
    """Diarize into ``out_dir``, without ASR: samples, ``diarization.json`` and NEXT_STEPS.md.

    No transcript is produced - this is for a caller (MacParakeet) that does its
    own speech recognition and only wants speaker turns, name suggestions and
    per-speaker samples from paraspeakrs.
    """
    out_dir = _check_out_dir(out_dir)
    summary = workspace.diarize(Path(audio_path))
    files, report = _stage(workspace, summary, out_dir, kind="diarization")
    sample_files = {row.speaker: files[row.speaker].name if row.speaker in files else None for row in summary.speakers}
    workspace.write_diarization_json(summary.job_id, out_dir / DIARIZATION_JSON, sample_files)
    report["diarization"] = str(out_dir / DIARIZATION_JSON)
    return report


def _check_out_dir(out_dir: Path) -> Path:
    out_dir = Path(out_dir).expanduser().resolve()
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ValueError(f"output directory {out_dir} is not empty - pick a new one, or run `agent apply` on it")
    return out_dir


def _stage(workspace: LabelingWorkspace, summary: JobSummary, out_dir: Path, *, kind: str) -> tuple[dict, dict]:
    """Write manifest.json, speaker sample montages and NEXT_STEPS.md - shared by
    ``prepare`` and ``diarize``, which differ only in the transcript/diarization
    file they write afterward and the report key that names it.

    Returns (files, report): ``files`` so the caller can build the same
    ``sample_files`` mapping its own output file needs, ``report`` as the common
    part of the JSON report, left for the caller to add its own output key to.
    """
    job_id = summary.job_id
    hints = _hints(workspace, job_id)

    speakers, files = {}, {}
    for row in summary.speakers:
        suggestion = _suggestion(row)
        speakers[row.speaker] = {"suggested_label": row.suggested_label if suggestion else None, "suggestion": suggestion}
        if not row.excluded:
            files[row.speaker] = out_dir / f"{row.speaker}{SEP}{_initial_name(row, suggestion)}.wav"
    workspace.speaker_montages(job_id, files)

    manifest: dict = {"version": 1, "job_id": job_id, "source": str(summary.source_path), "speakers": speakers}
    if kind != "transcript":
        manifest["kind"] = kind
    (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    rows = [
        {
            "speaker": row.speaker,
            "file": files[row.speaker].name,
            "suggested_label": speakers[row.speaker]["suggested_label"],
            "score": round(row.suggestion_score, 2),
            "talk_seconds": row.talk_seconds,
            "sample_lines": hints.get(row.speaker, []),
        }
        for row in summary.speakers
        if row.speaker in files
    ]
    (out_dir / NEXT_STEPS).write_text(_next_steps(out_dir, rows), encoding="utf-8")
    report = {
        "out_dir": str(out_dir),
        "job_id": job_id,
        "reused": summary.reused,
        "speakers": rows,
        "next": f"Ask the user to rename the samples in {out_dir} (see {NEXT_STEPS}), "
        f"then run: paraspeakrs agent apply {out_dir}",
    }
    return files, report


def apply(workspace: LabelingWorkspace, out_dir: Path, *, dry_run: bool = False) -> dict:
    out_dir = Path(out_dir).expanduser().resolve()
    manifest_path = out_dir / MANIFEST
    if not manifest_path.is_file():
        raise ValueError(
            f"{out_dir} has no {MANIFEST} - was it made by "
            "`paraspeakrs agent transcribe` or `paraspeakrs agent diarize`?"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    job_id = manifest["job_id"]
    kind = manifest.get("kind", "transcript")
    decisions = plan(out_dir, manifest["speakers"])

    lines = None
    if not dry_run:
        current = {row.speaker: row.assigned_label for row in workspace.speaker_rows(job_id)}
        for d in decisions:
            if current.get(d.speaker) is not None and (current[d.speaker] != d.name or not d.teach):
                workspace.unassign_speaker(job_id, d.speaker)
            if d.name is not None:
                workspace.label_speaker(job_id, d.speaker, d.name, teach=d.teach)
            workspace.set_speaker_excluded(job_id, d.speaker, d.action == "dropped")
        sample_files = _sample_files(out_dir, manifest["speakers"])
        if kind == "diarization":
            workspace.write_diarization_json(job_id, out_dir / DIARIZATION_JSON, sample_files)
        else:
            _, lines = workspace.write_transcript(job_id, out_dir / TRANSCRIPT)
            workspace.write_transcript_json(job_id, out_dir / TRANSCRIPT_JSON, sample_files)

    by_name: dict[str, list[str]] = {}
    for d in decisions:
        if d.name is not None:
            by_name.setdefault(d.name, []).append(d.speaker)
    result = {
        "dry_run": dry_run,
        "job_id": job_id,
        "speakers": [{"speaker": d.speaker, "action": d.action, "name": d.name} for d in decisions],
        "merged": {name: ids for name, ids in by_name.items() if len(ids) > 1},
    }
    if kind == "diarization":
        result["diarization"] = str(out_dir / DIARIZATION_JSON)
    else:
        result["transcript"] = str(out_dir / TRANSCRIPT)
    if lines is not None:
        result["lines"] = lines
    return result


def plan(out_dir: Path, speakers: dict[str, dict]) -> list[Decision]:
    """Read the directory into one decision per speaker, or fail naming every bad file."""
    chosen: dict[str, str] = {}
    problems = []
    # Longest ID first, so L_SPEAKER_00 is never taken for a SPEAKER_00 prefix.
    ids = sorted(speakers, key=len, reverse=True)
    for path in sorted(out_dir.glob("*.wav")):
        speaker = next((s for s in ids if path.stem.startswith(s + SEP)), None)
        rest = path.stem[len(speaker) + len(SEP):].strip() if speaker else ""
        if speaker is None:
            problems.append(f"{path.name}: does not start with a speaker ID and '{SEP}' ({', '.join(sorted(speakers))})")
        elif not rest:
            problems.append(f"{path.name}: the name after '{SEP}' is empty")
        elif speaker in chosen:
            problems.append(f"{path.name}: a second file for {speaker} (already {speaker}{SEP}{chosen[speaker]}.wav)")
        else:
            chosen[speaker] = rest
    if problems:
        raise ValueError("cannot apply names, nothing was changed:\n  " + "\n  ".join(problems))

    decisions = []
    for speaker in sorted(speakers):
        rest = chosen.get(speaker)
        suggestion = speakers[speaker].get("suggestion")
        if rest is None:
            decisions.append(Decision(speaker, "dropped", None))
        elif rest == UNNAMED:
            decisions.append(Decision(speaker, "unnamed", None))
        elif suggestion is not None and rest == suggestion:
            decisions.append(Decision(speaker, "suggestion", speakers[speaker]["suggested_label"]))
        else:
            decisions.append(Decision(speaker, "named", rest))
    return decisions


def _sample_files(out_dir: Path, speakers: dict[str, dict]) -> dict[str, str]:
    """Each speaker's current sample filename on disk, by the ID-prefix match plan() uses.

    Called after plan() has already validated the directory (or apply would have
    aborted before reaching this), so every match here is unambiguous.
    """
    ids = sorted(speakers, key=len, reverse=True)
    files = {}
    for path in sorted(out_dir.glob("*.wav")):
        speaker = next((s for s in ids if path.stem.startswith(s + SEP)), None)
        if speaker is not None:
            files[speaker] = path.name
    return files


def _suggestion(row: SpeakerRow) -> str | None:
    """``<name>_<score>``, the exact suffix apply will recognize as a guess left untouched."""
    if row.assigned_label is not None or row.suggested_label is None:
        return None
    return f"{_safe(row.suggested_label)}_{row.suggestion_score:.2f}"


def _initial_name(row: SpeakerRow, suggestion: str | None) -> str:
    if row.assigned_label is not None:
        return _safe(row.assigned_label)
    return suggestion or UNNAMED


def _safe(name: str) -> str:
    return name.replace("/", "-").replace("\0", "").strip() or UNNAMED


def _hints(workspace: LabelingWorkspace, job_id: str) -> dict[str, list[str]]:
    """Each speaker's first few lines - often enough for an agent to guess who it is."""
    hints: dict[str, list[str]] = {}
    for line in workspace.transcript_lines(job_id):
        bucket = hints.setdefault(line.speaker, [])
        if not line.excluded and len(bucket) < HINT_LINES:
            bucket.append(f"[{format_time(line.start)}] {line.text}")
    return hints


def _next_steps(out_dir: Path, rows: list[dict]) -> str:
    parts = [
        "# Name the speakers\n",
        "Listen to each `.wav` (a few takes of one speaker, separated by a beep) and rename it:\n",
        f"- `SPEAKER_00{SEP}{UNNAMED}.wav` → `SPEAKER_00{SEP}Alice.wav` names the speaker.",
        f"- Keep the part before `{SEP}`. Only replace what comes after it.",
        "- `…__Alice_0.63.wav` is a voice-library guess (score 0.63). Leave it to accept it, or rename it.",
        "- Give two files the same name to merge them into one person.",
        "- Delete a file to drop that speaker's lines from the transcript.",
        f"- Leave `{UNNAMED}` to keep the raw speaker ID.\n",
        f"Then run: `paraspeakrs agent apply {out_dir}` (safe to re-run after more renames).\n",
        "## Speakers\n",
    ]
    for row in rows:
        parts.append(f"### {row['file']} ({row['talk_seconds']:.0f} s)\n")
        parts.extend(f"    {line}" for line in row["sample_lines"])
        parts.append("")
    return "\n".join(parts)
