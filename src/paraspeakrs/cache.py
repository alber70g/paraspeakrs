from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import DiarizationSegment

# Embeddings from different diarization backends are not comparable: senko emits
# 192-dim CAM++ centroids, speakrs 256-dim WeSpeaker ones. Cached speakers are
# therefore filed under the backend that produced them.
EMBEDDING_NAMESPACES = {"senko": "senko:192", "speakrs": "speakrs:256"}

# The flat pre-namespace {"speakers": ...} layout was only ever written by senko.
LEGACY_NAMESPACE = EMBEDDING_NAMESPACES["senko"]


def embedding_namespace(diar_backend: str) -> str:
    return EMBEDDING_NAMESPACES[diar_backend]


@dataclass(frozen=True)
class CacheMember:
    """One recording of a voice that contributes to a name's centroid.

    ``job_id``/``speaker_id`` point back at the job the voice came from so a
    stored voice can be played back; they are ``None`` for members migrated from
    the old averaged-only layout, which kept no provenance.
    """

    job_id: str | None
    speaker_id: str | None
    embedding: list[float]
    added: str | None
    weight: float = 1.0

    @property
    def playable(self) -> bool:
        return self.job_id is not None and self.speaker_id is not None

    @property
    def ref(self) -> tuple[str | None, str | None]:
        return (self.job_id, self.speaker_id)


class SpeakerCache:
    """Named voice prints, kept as their contributing members rather than a mean.

    A running weighted average cannot be undone: once two voices are folded into
    one centroid there is no way back to either. Keeping the members and deriving
    the centroid from them makes folding reversible, and lets a stored voice be
    played back by following its member to the job it came from.
    """

    def __init__(self, path: Path, threshold: float = 0.75, namespace: str = LEGACY_NAMESPACE) -> None:
        self.path = path
        self.threshold = threshold
        self.namespace = namespace
        self._cache: dict | None = None
        self._stat_key: tuple[int, int] | None = None

    def resolve(self, segments: list[DiarizationSegment]) -> dict[str, str | None]:
        return {segment.speaker: None for segment in segments}

    def resolve_embeddings(self, embeddings: dict[str, list[float]]) -> dict[str, str | None]:
        """Best name per speaker, or None when nothing clears the threshold."""
        return {
            speaker: (label if score >= self.threshold else None)
            for speaker, (label, score) in self.suggest_embeddings(embeddings).items()
        }

    def suggest_embeddings(self, embeddings: dict[str, list[float]]) -> dict[str, tuple[str | None, float]]:
        """Best (name, score) per speaker, threshold *not* applied.

        The UI shows a weak match as a weak match rather than as nothing at all,
        so the score has to survive the lookup.
        """
        speakers = self._speakers()
        return {speaker: _best_match(embedding, speakers) for speaker, embedding in embeddings.items()}

    def names(self) -> list[str]:
        return sorted(self._speakers())

    def members(self, name: str) -> list[CacheMember]:
        entry = self._speakers().get(name)
        return [_member(payload) for payload in entry["members"]] if entry else []

    def similarity(self, name: str, embedding: list[float]) -> float | None:
        """How much ``embedding`` sounds like the stored ``name``, or None if unknown."""
        entry = self._speakers().get(name)
        return _cosine(embedding, entry["embedding"]) if entry else None

    def record_embeddings(
        self,
        labels: dict[str, str],
        embeddings: dict[str, list[float]],
        *,
        job_id: str | None = None,
    ) -> None:
        cache = copy.deepcopy(self._load())
        speakers = cache.setdefault("speakers_by_source", {}).setdefault(self.namespace, {})
        changed = False
        for speaker_id, label in labels.items():
            embedding = embeddings.get(speaker_id)
            if not embedding or not label:
                continue
            _fold_into(speakers, label, embedding, job_id, speaker_id)
            changed = True
        if changed:
            self._save(cache)

    def fold(self, name: str, embedding: list[float], *, job_id: str, speaker_id: str) -> None:
        """Add one job's speaker to ``name``, replacing any earlier take of it.

        Re-labeling the same speaker must not stack two members for one voice, or
        a speaker renamed back and forth would weigh double in its own centroid.
        """
        self.record_embeddings({speaker_id: name}, {speaker_id: embedding}, job_id=job_id)

    def unfold(self, name: str, job_id: str, speaker_id: str) -> bool:
        """Pull one member back out, recomputing the centroid from what is left.

        Returns False when the member is not there. Removing the last member
        drops the name entirely - an empty voice matches nothing, and leaving it
        listed only invites folding a stranger into it.
        """
        cache = copy.deepcopy(self._load())
        speakers = cache.get("speakers_by_source", {}).get(self.namespace, {})
        entry = speakers.get(name)
        if entry is None:
            return False
        kept = [m for m in entry["members"] if (m.get("job_id"), m.get("speaker_id")) != (job_id, speaker_id)]
        if len(kept) == len(entry["members"]):
            return False
        if kept:
            speakers[name] = _entry(kept)
        else:
            del speakers[name]
        self._save(cache)
        return True

    def merge_names(self, source: str, target: str) -> bool:
        """Fold every member of ``source`` into ``target`` and drop ``source``."""
        if source == target:
            return False
        cache = copy.deepcopy(self._load())
        speakers = cache.get("speakers_by_source", {}).get(self.namespace, {})
        moved = speakers.pop(source, None)
        if moved is None:
            return False
        existing = speakers.get(target)
        members = (existing["members"] if existing else []) + moved["members"]
        speakers[target] = _entry(members)
        self._save(cache)
        return True

    def delete(self, name: str) -> bool:
        cache = copy.deepcopy(self._load())
        speakers = cache.get("speakers_by_source", {}).get(self.namespace, {})
        if speakers.pop(name, None) is None:
            return False
        self._save(cache)
        return True

    def _speakers(self) -> dict:
        return self._load().get("speakers_by_source", {}).get(self.namespace, {})

    def _load(self) -> dict:
        """Memoized parse, re-read when the file changes. Shared — copy before mutating."""
        if not self.path.exists():
            self._cache = None
            self._stat_key = None
            return {"speakers_by_source": {}}
        stat = self.path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        cache = self._cache
        if cache is None or self._stat_key != key:
            cache = _migrate(json.loads(self.path.read_text(encoding="utf-8")))
            self._cache = cache
            self._stat_key = key
        return cache

    def _save(self, cache: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
        stat = self.path.stat()
        self._cache = copy.deepcopy(cache)
        self._stat_key = (stat.st_mtime_ns, stat.st_size)


def _fold_into(
    speakers: dict,
    label: str,
    embedding: list[float],
    job_id: str | None,
    speaker_id: str | None,
) -> None:
    entry = speakers.get(label)
    members = list(entry["members"]) if entry else []
    if job_id is not None and speaker_id is not None:
        members = [m for m in members if (m.get("job_id"), m.get("speaker_id")) != (job_id, speaker_id)]
    members.append(
        {
            "job_id": job_id,
            "speaker_id": speaker_id,
            "embedding": _normalize(embedding),
            "added": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "weight": 1.0,
        }
    )
    speakers[label] = _entry(members)


def _entry(members: list[dict]) -> dict:
    return {"members": members, "embedding": _centroid(members)}


def _centroid(members: list[dict]) -> list[float]:
    """Normalized weighted mean of the members' unit vectors.

    Every side of the sum has to be a unit vector: a raw embedding's norm (~4.5)
    otherwise outweighs the others and the newest member hijacks the centroid,
    dropping earlier ones below the match threshold.
    """
    width = len(members[0]["embedding"])
    total = 0.0
    summed = [0.0] * width
    for member in members:
        vector = _normalize(member["embedding"])
        _require_same_width(summed, vector)
        weight = float(member.get("weight", 1.0))
        total += weight
        for index in range(width):
            summed[index] += vector[index] * weight
    if total == 0:
        return _normalize(summed)
    return _normalize([value / total for value in summed])


def _member(payload: dict) -> CacheMember:
    return CacheMember(
        job_id=payload.get("job_id"),
        speaker_id=payload.get("speaker_id"),
        embedding=payload["embedding"],
        added=payload.get("added"),
        weight=float(payload.get("weight", 1.0)),
    )


def _best_match(embedding: list[float], speakers: dict) -> tuple[str | None, float]:
    best_label: str | None = None
    best_score = -1.0
    for label, payload in speakers.items():
        score = _cosine(embedding, payload["embedding"])
        if score > best_score:
            best_label = label
            best_score = score
    return best_label, best_score


def _migrate(cache: dict) -> dict:
    """Bring an older cache up to the members layout.

    Two older shapes exist: the flat pre-namespace {"speakers": ...}, only ever
    written by senko, and the namespaced {"embedding", "count"} entry that kept
    no provenance. The latter becomes a single anonymous member carrying its
    count as weight - it still matches and can still be folded into, but there
    is nothing to play back or to unfold, because that history was never stored.
    """
    legacy = cache.pop("speakers", None)
    if legacy:
        cache.setdefault("speakers_by_source", {}).setdefault(LEGACY_NAMESPACE, {}).update(legacy)
    for speakers in cache.get("speakers_by_source", {}).values():
        for label, payload in speakers.items():
            if "members" not in payload:
                speakers[label] = _entry(
                    [
                        {
                            "job_id": None,
                            "speaker_id": None,
                            "embedding": _normalize(payload["embedding"]),
                            "added": None,
                            "weight": float(payload.get("count", 1)),
                        }
                    ]
                )
    return cache


def _require_same_width(left: list[float], right: list[float]) -> int:
    """Embeddings of different widths are from different models and must never be
    compared. Truncating to the shorter one returns a plausible-looking score for
    unrelated vectors, which silently mislabels speakers."""
    if len(left) != len(right):
        raise ValueError(f"embedding dimension mismatch: {len(left)} vs {len(right)}")
    return len(left)


def _cosine(left: list[float], right: list[float]) -> float:
    width = _require_same_width(left, right)
    if width == 0:
        return -1.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return -1.0
    dot = sum(left[index] * right[index] for index in range(width))
    return dot / (left_norm * right_norm)


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]
