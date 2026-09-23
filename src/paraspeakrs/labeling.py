from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .mcp_store import ArtifactStore, apply_labels, fingerprint_file, talk_seconds
from .models import PipelineArtifacts
from .pipeline import ProgressCallback, TranscriptionPipeline
from .snippets import export_label_snippets, montage_segments, write_montage
from .transcript_txt import line_key, render_frontmatter, render_txt, utterances


@dataclass
class SpeakerRow:
    """One diarized speaker, with what you decided and what the cache thinks.

    ``assigned_label`` is a decision - it was typed or accepted by a person, and
    it is the only thing that reaches a transcript. ``suggested_label`` is a
    guess from the voice cache that has cleared the match threshold, offered for
    acceptance and never applied on its own. ``suggestion_score`` is the nearest
    cached voice's cosine whether or not it cleared, so a weak match can be
    shown as weak instead of as nothing at all.
    """

    speaker: str
    assigned_label: str | None
    suggested_label: str | None
    suggestion_score: float
    talk_seconds: float
    excluded: bool = False

    @property
    def unnamed(self) -> bool:
        """Still waiting for a name. A removed speaker is not waiting for anything."""
        return self.assigned_label is None and not self.excluded


@dataclass
class VoiceMember:
    """One job's speaker as it is stored inside a named voice."""

    job_id: str | None
    speaker_id: str | None
    source: str | None
    added: str | None
    playable: bool


@dataclass
class Voice:
    name: str
    members: list[VoiceMember]


@dataclass
class JobSummary:
    job_id: str
    source_path: Path | None
    duration_seconds: float
    num_speakers: int
    speakers: list[SpeakerRow]
    note: str = ""
    reused: bool = False
    recorded_on: str | None = None

    @property
    def title(self) -> str:
        return self.source_path.stem if self.source_path else self.job_id

    @property
    def named_speakers(self) -> list[str]:
        """Who took part, by name, in order of first naming - removed speakers left out."""
        return list(dict.fromkeys(r.assigned_label for r in self.speakers if r.assigned_label and not r.excluded))

    @property
    def header(self) -> str:
        """``title · date · names``, the line that says which meeting this was."""
        return " · ".join(p for p in (self.title, self.recorded_on, ", ".join(self.named_speakers)) if p)


@dataclass
class TranscriptLine:
    """One rendered transcript line, with whether a person removed it."""

    speaker: str
    name: str
    start: float
    end: float
    text: str
    excluded: bool


class LabelingWorkspace:
    """Diarize / label / transcribe flow, front-end agnostic.

    Wraps a :class:`TranscriptionPipeline` and an :class:`ArtifactStore` so both
    the MCP server and the terminal UI drive the exact same operations and share
    the same on-disk jobs.
    """

    def __init__(self, pipeline: TranscriptionPipeline, store: ArtifactStore) -> None:
        self.pipeline = pipeline
        self.store = store

    def transcribe(
        self,
        audio_path: Path,
        *,
        progress: ProgressCallback | None = None,
        force: bool = False,
        single_speaker_channels: frozenset[str] = frozenset(),
    ) -> JobSummary:
        """Transcribe ``audio_path``, reusing an existing job for identical audio.

        ASR and diarization over a meeting cost minutes, and re-running them
        produces the same segments; a job whose source hashed the same is
        returned as-is. Pass ``force=True`` to transcribe regardless.
        """
        source = Path(audio_path).expanduser()
        if not source.is_file():
            raise ValueError(f"audio file not found: {source}")

        fingerprint = fingerprint_file(source)
        if not force:
            existing = self.store.find_by_fingerprint(fingerprint)
            if existing is not None:
                job_id, artifacts = existing
                if progress is not None:
                    progress("reuse", f"reusing job {job_id} for identical audio", 100)
                return self._summary(job_id, artifacts, reused=True)

        job_id = str(uuid4())
        artifacts = self.pipeline.run_with_artifacts(
            work_dir=self.store.work_dir(job_id),
            file_path=source,
            job_id=job_id,
            progress=progress,
            single_speaker_channels=single_speaker_channels,
        )
        artifacts = artifacts.model_copy(update={"source_fingerprint": fingerprint})
        self.store.save(job_id, artifacts)
        self.store.prune_audio(job_id)
        return self._summary(job_id, artifacts)

    def list_jobs(self) -> list[JobSummary]:
        return [self._summary(job_id, artifacts) for job_id, artifacts in self.store.list_jobs()]

    def job_summary(self, job_id: str) -> JobSummary:
        artifacts = self._load(job_id)
        return self._summary(job_id, artifacts)

    def speaker_rows(self, job_id: str) -> list[SpeakerRow]:
        return self._speaker_rows(self._load(job_id))

    def speaker_sample(self, job_id: str, speaker_id: str) -> Path:
        artifacts = self._load(job_id)
        segments = [seg for seg in artifacts.diarization if seg.speaker == speaker_id]
        if not segments:
            raise ValueError(f"unknown speaker {speaker_id!r} for job {job_id}")

        out_dir = self.store.work_dir(job_id) / "snippets" / speaker_id
        cached = sorted(out_dir.glob(f"{speaker_id}-*.wav"))
        if cached:
            return cached[0].resolve()

        audio_path, temporary = self._sample_source(artifacts, job_id)
        try:
            snippets = export_label_snippets(
                self.pipeline.audio,
                audio_path,
                out_dir,
                segments,
                max_per_speaker=1,
                max_seconds=10.0,
                min_seconds=1.5,
            )
        finally:
            if temporary:
                audio_path.unlink(missing_ok=True)
        paths = snippets.get(speaker_id, [])
        if not paths:
            raise ValueError(f"no usable audio sample for speaker {speaker_id!r}")
        return paths[0].resolve()

    def speaker_montages(self, job_id: str, targets: dict[str, Path]) -> dict[str, Path]:
        """Write one montage per speaker in ``targets``, cut from a single source.

        A pruned job rebuilds its audio from the original recording, which costs
        a full decode; doing that once rather than once per speaker is the point
        of taking them all together.
        """
        artifacts = self._load(job_id)
        for speaker_id in targets:
            self._require_speaker(job_id, speaker_id)
        audio_path, temporary = self._sample_source(artifacts, job_id)
        try:
            for speaker_id, target in targets.items():
                segments = [seg for seg in artifacts.diarization if seg.speaker == speaker_id]
                write_montage(self.pipeline.audio, audio_path, target, montage_segments(segments))
        finally:
            if temporary:
                audio_path.unlink(missing_ok=True)
        return targets

    def _sample_source(self, artifacts: PipelineArtifacts, job_id: str) -> tuple[Path, bool]:
        """Audio to cut a snippet from, rebuilt from the original if pruned.

        Returns the path and whether it is a throwaway that the caller should
        delete, so pruned jobs do not silently grow their working audio back.
        """
        normalized = Path(artifacts.normalized_audio_path)
        if normalized.is_file():
            return normalized, False

        source = artifacts.source_path
        if source is None or not Path(source).is_file():
            raise ValueError(
                f"job {job_id} keeps only its transcript, and the original recording is "
                f"no longer at {source} - samples cannot be rebuilt without it"
            )
        rebuilt = self.store.work_dir(job_id) / "playback.wav"
        return Path(self.pipeline.audio.normalize_mono_16khz(Path(source), rebuilt)), True

    def label_speaker(self, job_id: str, speaker_id: str, name: str, *, teach: bool = True) -> list[SpeakerRow]:
        """Assign ``name`` to this job's speaker, and by default remember the voice.

        ``teach=False`` names the speaker here without folding them into the
        cached voice - the escape hatch for two different people who genuinely
        share a name, whose embeddings would otherwise average into a centroid
        that matches neither.
        """
        artifacts = self._require_speaker(job_id, speaker_id)
        # Naming is about this job and needs no embedding; only teaching the
        # cross-job voice cache does. A diarizer that returned segments but no
        # centroids still lists its speakers in the table, and those have to stay
        # nameable - refusing them makes the labeling step look broken.
        embedding = artifacts.speaker_embeddings.get(speaker_id)
        if teach and embedding is not None:
            self.pipeline.speaker_cache.fold(name, embedding, job_id=job_id, speaker_id=speaker_id)
        return self._assign(job_id, artifacts, {speaker_id: name})

    def fold_similarity(self, job_id: str, speaker_id: str, name: str) -> float | None:
        """How much this speaker sounds like the voice already stored as ``name``.

        None when the name is new or the speaker has no embedding - in both cases
        there is nothing to fold into and nothing to warn about.
        """
        artifacts = self._load(job_id)
        embedding = artifacts.speaker_embeddings.get(speaker_id)
        if embedding is None:
            return None
        return self.pipeline.speaker_cache.similarity(name, embedding)

    def accept_suggestion(self, job_id: str, speaker_id: str) -> list[SpeakerRow]:
        row = next((r for r in self.speaker_rows(job_id) if r.speaker == speaker_id), None)
        if row is None or row.suggested_label is None:
            raise ValueError(f"no suggestion to accept for {speaker_id!r}")
        return self.label_speaker(job_id, speaker_id, row.suggested_label)

    def accept_all_suggestions(self, job_id: str) -> list[SpeakerRow]:
        """Accept every suggestion on speakers that have not been named by hand.

        An assigned name is a decision and is never overwritten by a guess, so a
        second press of accept-all after some manual naming is a no-op on those.
        """
        rows = self.speaker_rows(job_id)
        for row in rows:
            if row.unnamed and row.suggested_label is not None:
                rows = self.label_speaker(job_id, row.speaker, row.suggested_label)
        return rows

    def merge_speakers(self, job_id: str, speaker_ids: list[str], name: str) -> list[SpeakerRow]:
        """Give several of this job's speakers one name, folding them into one voice.

        Stereo recordings split one person across channels, so the same voice
        routinely arrives as L_SPEAKER_00 and R_SPEAKER_00; naming them together
        is the common case, not an edge case.
        """
        if not speaker_ids:
            raise ValueError("no speakers to merge")
        artifacts = self._load(job_id)
        for speaker_id in speaker_ids:
            self._require_speaker(job_id, speaker_id)
        cache = self.pipeline.speaker_cache
        for speaker_id in speaker_ids:
            embedding = artifacts.speaker_embeddings.get(speaker_id)
            if embedding is not None:
                cache.fold(name, embedding, job_id=job_id, speaker_id=speaker_id)
        return self._assign(job_id, artifacts, dict.fromkeys(speaker_ids, name))

    def unassign_speaker(self, job_id: str, speaker_id: str) -> list[SpeakerRow]:
        """Take the name back off a speaker and out of the voice it taught.

        Leaving the embedding folded in would keep a wrong name pulling on that
        centroid long after it stopped being shown anywhere.
        """
        artifacts = self._require_speaker(job_id, speaker_id)
        name = artifacts.speaker_labels.get(speaker_id)
        if name is not None:
            self.pipeline.speaker_cache.unfold(name, job_id, speaker_id)
        labels = {k: v for k, v in artifacts.speaker_labels.items() if k != speaker_id}
        artifacts = artifacts.model_copy(update={"speaker_labels": labels})
        self.store.save(job_id, artifacts)
        return self._speaker_rows(artifacts)

    def toggle_speaker_excluded(self, job_id: str, speaker_id: str) -> list[SpeakerRow]:
        """Remove a speaker from the transcript, or put them back.

        For voices that were recorded but were never part of the meeting - a
        child in the room while the call's mic was muted. Nothing is deleted:
        the lines stay in the job and only the rendered transcript skips them.
        """
        artifacts = self._require_speaker(job_id, speaker_id)
        excluded = [s for s in artifacts.excluded_speakers if s != speaker_id]
        if len(excluded) == len(artifacts.excluded_speakers):
            excluded.append(speaker_id)
        artifacts = artifacts.model_copy(update={"excluded_speakers": excluded})
        self.store.save(job_id, artifacts)
        return self._speaker_rows(artifacts)

    def set_speaker_excluded(self, job_id: str, speaker_id: str, excluded: bool) -> list[SpeakerRow]:
        """Remove a speaker from the transcript or put them back, stated rather than toggled.

        The agent flow re-applies a whole directory's state on every run, which a
        toggle cannot do without first reading which way it currently points.
        """
        artifacts = self._require_speaker(job_id, speaker_id)
        if (speaker_id in artifacts.excluded_speakers) != excluded:
            return self.toggle_speaker_excluded(job_id, speaker_id)
        return self._speaker_rows(artifacts)

    def toggle_line_excluded(self, job_id: str, speaker_id: str, start: float) -> bool:
        """Remove one transcript line, or put it back. Returns whether it is now removed."""
        artifacts = self._require_speaker(job_id, speaker_id)
        key = line_key(speaker_id, start)
        excluded = [tuple(k) for k in artifacts.excluded_lines if line_key(*k) != key]
        removed = len(excluded) == len(artifacts.excluded_lines)
        if removed:
            excluded.append(key)
        self.store.save(job_id, artifacts.model_copy(update={"excluded_lines": excluded}))
        return removed

    def transcript_lines(self, job_id: str) -> list[TranscriptLine]:
        """Every line of the transcript, removed ones included and marked as such."""
        artifacts = self._load(job_id)
        speakers = set(artifacts.excluded_speakers)
        lines = {line_key(*k) for k in artifacts.excluded_lines}
        return [
            TranscriptLine(
                speaker=utt.speaker,
                name=utt.name,
                start=utt.start,
                end=utt.end,
                text=utt.text,
                excluded=utt.speaker in speakers or utt.key in lines,
            )
            for utt in utterances(apply_labels(artifacts.result, self._labels(artifacts)))
        ]

    def job_note(self, job_id: str) -> str:
        """The note written about this meeting, or "" when none was written."""
        return self.store.note(job_id)

    def set_job_note(self, job_id: str, note: str) -> str:
        """Record what this meeting was about, beside the job it belongs to.

        Diarization answers who spoke; it cannot answer why the recording
        mattered. A note is the only place that survives with the job, so it is
        stored with it rather than in a file the next transcript overwrites.
        """
        self._load(job_id)
        note = note.strip()
        self.store.set_note(job_id, note)
        return note

    def delete_job(self, job_id: str) -> None:
        """Forget a job. The voices it taught stay - they are the durable part.

        A deleted job cannot be played back any more, so its members become
        unplayable, but the names they contributed to are still how every future
        recording gets recognized.
        """
        if not self.store.delete(job_id):
            raise ValueError(f"unknown job_id {job_id!r}")

    def embedding_mismatch(self, job_id: str) -> tuple[int, str] | None:
        """(width, namespace) when this job's voice prints cannot reach the cache.

        Backends emit different-width embeddings and are cached separately, so a
        job diarized with one against a cache built by another can never produce
        a suggestion. That is worth saying out loud - an empty suggestion column
        otherwise reads as "nobody here is known" rather than "I cannot look".
        """
        artifacts = self._load(job_id)
        widths = {len(embedding) for embedding in artifacts.speaker_embeddings.values()}
        namespace = self.pipeline.speaker_cache.namespace
        _, _, expected = namespace.partition(":")
        if not widths or not expected.isdigit() or int(expected) in widths:
            return None
        return (sorted(widths)[0], namespace)

    def voices(self) -> list[Voice]:
        """Every cached voice with the recordings it was learned from."""
        cache = self.pipeline.speaker_cache
        sources = {job_id: artifacts.source_path for job_id, artifacts in self.store.list_jobs()}
        return [
            Voice(
                name=name,
                members=[
                    VoiceMember(
                        job_id=member.job_id,
                        speaker_id=member.speaker_id,
                        source=_source_name(sources.get(member.job_id or "")),
                        added=member.added,
                        playable=member.playable and member.job_id in sources,
                    )
                    for member in cache.members(name)
                ],
            )
            for name in cache.names()
        ]

    def merge_voices(self, source: str, target: str) -> None:
        """Fold one cached name into another, and rename it wherever it was assigned."""
        if not self.pipeline.speaker_cache.merge_names(source, target):
            raise ValueError(f"unknown voice {source!r}")
        for job_id, artifacts in self.store.list_jobs():
            renamed = {k: (target if v == source else v) for k, v in artifacts.speaker_labels.items()}
            if renamed != artifacts.speaker_labels:
                self.store.save(job_id, artifacts.model_copy(update={"speaker_labels": renamed}))

    def unfold_voice(self, name: str, job_id: str, speaker_id: str) -> None:
        """Pull one recording out of a voice, leaving that speaker unnamed again."""
        if not self.pipeline.speaker_cache.unfold(name, job_id, speaker_id):
            raise ValueError(f"{name!r} was not learned from {speaker_id} in job {job_id}")
        try:
            artifacts = self._load(job_id)
        except ValueError:
            return
        labels = {k: v for k, v in artifacts.speaker_labels.items() if k != speaker_id}
        if labels != artifacts.speaker_labels:
            self.store.save(job_id, artifacts.model_copy(update={"speaker_labels": labels}))

    def delete_voice(self, name: str) -> None:
        if not self.pipeline.speaker_cache.delete(name):
            raise ValueError(f"unknown voice {name!r}")

    def _require_speaker(self, job_id: str, speaker_id: str) -> PipelineArtifacts:
        artifacts = self._load(job_id)
        if speaker_id not in talk_seconds(artifacts):
            known = ", ".join(sorted(talk_seconds(artifacts))) or "none"
            raise ValueError(f"unknown speaker {speaker_id!r} for job {job_id} (this job has: {known})")
        return artifacts

    def _assign(self, job_id: str, artifacts: PipelineArtifacts, names: dict[str, str]) -> list[SpeakerRow]:
        artifacts = artifacts.model_copy(update={"speaker_labels": {**artifacts.speaker_labels, **names}})
        self.store.save(job_id, artifacts)
        return self._speaker_rows(artifacts)

    def write_transcript(self, job_id: str, output_path: Path) -> tuple[Path, int]:
        """Write the transcript, returning where it went and how many spoken lines it has.

        The count is of transcript lines only. The note's frontmatter is a header
        about the file, and counting it would report a number that grows when
        somebody edits the note rather than when anything was said.
        """
        header, body = self._transcript_parts(job_id)
        out = Path(output_path).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(header + body, encoding="utf-8")
        return out.resolve(), body.count("\n")

    def transcript_text(self, job_id: str) -> str:
        header, body = self._transcript_parts(job_id)
        return header + body

    def _transcript_parts(self, job_id: str) -> tuple[str, str]:
        """(frontmatter, transcript) for a job - empty frontmatter when it has no note.

        The note rides along as YAML frontmatter because a transcript is read
        weeks later out of context: who said what is in the body, and why the
        meeting happened is only ever in the note.
        """
        artifacts = self._load(job_id)
        body = render_txt(
            apply_labels(artifacts.result, self._labels(artifacts)),
            excluded_speakers=artifacts.excluded_speakers,
            excluded_lines=artifacts.excluded_lines,
        )
        summary = self._summary(job_id, artifacts)
        header = render_frontmatter(
            summary.note,
            title=summary.title,
            date=summary.recorded_on,
            speakers=summary.named_speakers,
        )
        return header, body

    def _summary(self, job_id: str, artifacts: PipelineArtifacts, reused: bool = False) -> JobSummary:
        return JobSummary(
            job_id=job_id,
            source_path=artifacts.source_path,
            duration_seconds=artifacts.result.duration_seconds,
            num_speakers=artifacts.result.num_speakers,
            speakers=self._speaker_rows(artifacts),
            note=self.store.note(job_id),
            reused=reused,
            recorded_on=_recorded_on(artifacts.source_path, self.store.root / job_id),
        )

    def _labels(self, artifacts: PipelineArtifacts) -> dict[str, str | None]:
        """Names for this job's speaker IDs - only the ones a person assigned.

        A cache match is a suggestion, and a suggestion that has not been
        accepted has no business appearing in a transcript as though it were
        established fact. Nothing a person did not confirm gets a name here;
        unnamed speakers keep their raw diarization ID.
        """
        return dict(artifacts.speaker_labels)

    def _speaker_rows(self, artifacts: PipelineArtifacts) -> list[SpeakerRow]:
        cache = self.pipeline.speaker_cache
        suggestions = cache.suggest_embeddings(artifacts.speaker_embeddings)
        durations = talk_seconds(artifacts)
        rows = []
        for speaker in sorted(durations):
            assigned = artifacts.speaker_labels.get(speaker)
            label, score = suggestions.get(speaker, (None, -1.0))
            rows.append(
                SpeakerRow(
                    speaker=speaker,
                    assigned_label=assigned,
                    suggested_label=label if score >= cache.threshold else None,
                    suggestion_score=max(score, 0.0),
                    talk_seconds=round(durations.get(speaker, 0.0), 1),
                    excluded=speaker in artifacts.excluded_speakers,
                )
            )
        return rows

    def _load(self, job_id: str) -> PipelineArtifacts:
        try:
            return self.store.load(job_id)
        except KeyError as exc:
            raise ValueError(f"unknown job_id {job_id!r}") from exc


def _recorded_on(source: Path | None, job_dir: Path) -> str | None:
    """The day the recording was made: the source file's birth time where the OS
    keeps one, else its mtime, else when the job was created."""
    for path in (source, job_dir):
        if path is None:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        stamp = getattr(stat, "st_birthtime", stat.st_mtime)
        return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")
    return None


def _source_name(path: Path | None) -> str | None:
    return path.name if path is not None else None
