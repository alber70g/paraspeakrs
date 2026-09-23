import json
import math

import pytest

from paraspeakrs.cache import LEGACY_NAMESPACE, SpeakerCache, embedding_namespace


def test_speaker_cache_resolves_recorded_embedding(tmp_path):
    cache = SpeakerCache(tmp_path / "speakers.json", threshold=0.75)
    cache.record_embeddings({"SPEAKER_00": "Albert"}, {"SPEAKER_00": [1.0, 0.0]})

    labels = cache.resolve_embeddings({"SPEAKER_03": [0.99, 0.01]})

    assert labels == {"SPEAKER_03": "Albert"}


def test_speaker_cache_rejects_low_similarity(tmp_path):
    cache = SpeakerCache(tmp_path / "speakers.json", threshold=0.75)
    cache.record_embeddings({"SPEAKER_00": "Albert"}, {"SPEAKER_00": [1.0, 0.0]})

    labels = cache.resolve_embeddings({"SPEAKER_03": [0.0, 1.0]})

    assert labels == {"SPEAKER_03": None}


def test_speaker_cache_sees_own_write_immediately(tmp_path):
    cache = SpeakerCache(tmp_path / "speakers.json", threshold=0.75)
    cache.resolve_embeddings({"SPEAKER_00": [1.0, 0.0]})  # populates the memo with an empty cache

    cache.record_embeddings({"SPEAKER_00": "Albert"}, {"SPEAKER_00": [1.0, 0.0]})
    labels = cache.resolve_embeddings({"SPEAKER_03": [0.99, 0.01]})

    assert labels == {"SPEAKER_03": "Albert"}


def test_speaker_cache_picks_up_external_rewrite(tmp_path):
    path = tmp_path / "speakers.json"
    cache = SpeakerCache(path, threshold=0.75)
    labels = cache.resolve_embeddings({"SPEAKER_00": [1.0, 0.0]})  # memoizes the missing-file state
    assert labels == {"SPEAKER_00": None}

    path.write_text(
        json.dumps({"speakers": {"Albert": {"embedding": [1.0, 0.0], "count": 1}}}),
        encoding="utf-8",
    )

    labels = cache.resolve_embeddings({"SPEAKER_03": [0.99, 0.01]})

    assert labels == {"SPEAKER_03": "Albert"}


def test_speaker_cache_discards_mutation_that_never_reached_disk(tmp_path):
    """A failed write must not leave a phantom label in the in-memory memo.

    record_embeddings mutates a dict before saving; if that dict aliased the memo,
    a crash mid-write would keep resolving to a name that was never persisted.
    """
    path = tmp_path / "speakers.json"
    cache = SpeakerCache(path, threshold=0.75)
    cache.record_embeddings({"SPEAKER_00": "Albert"}, {"SPEAKER_00": [1.0, 0.0]})

    def boom(_cache):
        raise OSError("disk full")

    cache._save = boom
    try:
        cache.record_embeddings({"SPEAKER_01": "Bianca"}, {"SPEAKER_01": [0.0, 1.0]})
    except OSError:
        pass
    del cache._save

    assert cache.resolve_embeddings({"SPEAKER_09": [0.0, 1.0]}) == {"SPEAKER_09": None}
    stored = json.loads(path.read_text())["speakers_by_source"][LEGACY_NAMESPACE]
    assert "Bianca" not in stored


def test_legacy_flat_cache_is_read_as_senko(tmp_path):
    """The pre-namespace layout was only ever written by senko, so labels recorded
    before the speakrs switch must keep resolving on the senko backend."""
    path = tmp_path / "speakers.json"
    path.write_text(
        json.dumps({"speakers": {"Albert": {"embedding": [1.0, 0.0], "count": 1}}}),
        encoding="utf-8",
    )
    cache = SpeakerCache(path, threshold=0.75, namespace=embedding_namespace("senko"))

    assert cache.resolve_embeddings({"SPEAKER_00": [0.99, 0.01]}) == {"SPEAKER_00": "Albert"}


def test_backends_do_not_see_each_others_speakers(tmp_path):
    """192-dim senko centroids and 256-dim speakrs ones are different vector spaces;
    a label learned under one must never match under the other."""
    path = tmp_path / "speakers.json"
    senko = SpeakerCache(path, threshold=0.75, namespace=embedding_namespace("senko"))
    senko.record_embeddings({"SPEAKER_00": "Albert"}, {"SPEAKER_00": [1.0, 0.0]})

    speakrs = SpeakerCache(path, threshold=0.75, namespace=embedding_namespace("speakrs"))

    assert speakrs.resolve_embeddings({"SPEAKER_00": [1.0, 0.0]}) == {"SPEAKER_00": None}
    assert senko.resolve_embeddings({"SPEAKER_00": [1.0, 0.0]}) == {"SPEAKER_00": "Albert"}


def test_same_name_on_two_speakers_keeps_both_resolving(tmp_path):
    """Naming a second diarized speaker must not silently un-name the first.

    One person picked up on two microphone channels is split into two diarized
    speakers whose raw embeddings sit ~0.5 cosine apart. Giving both the same
    name has to merge them into a centroid that still matches each one; if the
    merge weights the raw incoming vector (norm ~4.5) against the stored unit
    centroid, the centroid jumps onto the second speaker and the first one drops
    below the threshold and loses their name.
    """
    cache = SpeakerCache(tmp_path / "speakers.json", threshold=0.75)
    angle = math.acos(0.5)
    first = [4.5, 0.0]
    second = [4.5 * math.cos(angle), 4.5 * math.sin(angle)]

    cache.record_embeddings({"SPEAKER_00": "Albert"}, {"SPEAKER_00": first})
    cache.record_embeddings({"SPEAKER_01": "Albert"}, {"SPEAKER_01": second})

    labels = cache.resolve_embeddings({"SPEAKER_00": first, "SPEAKER_01": second})

    assert labels == {"SPEAKER_00": "Albert", "SPEAKER_01": "Albert"}


def test_mismatched_embedding_width_raises_instead_of_truncating(tmp_path):
    """Truncating to the shorter vector scores unrelated embeddings as similar,
    which silently attaches the wrong name to a speaker."""
    cache = SpeakerCache(tmp_path / "speakers.json", threshold=0.75)
    cache.record_embeddings({"SPEAKER_00": "Albert"}, {"SPEAKER_00": [1.0, 0.0]})

    with pytest.raises(ValueError, match="dimension mismatch"):
        cache.resolve_embeddings({"SPEAKER_00": [1.0, 0.0, 0.0, 0.0]})


def test_unfold_restores_the_remaining_voice_exactly(tmp_path):
    """Folding must be reversible, which a running average can never be.

    Two takes of a voice merge into one centroid; pulling the second back out has
    to leave the first as it was, not somewhere between the two. Anything less
    means a misfiled name permanently drags the centroid and every later match
    with it.
    """
    cache = SpeakerCache(tmp_path / "speakers.json", threshold=0.75)
    first = [1.0, 0.0, 0.0]
    second = [0.0, 1.0, 0.0]
    cache.fold("Albert", first, job_id="job-a", speaker_id="SPEAKER_00")
    cache.fold("Albert", second, job_id="job-b", speaker_id="SPEAKER_01")
    assert cache.resolve_embeddings({"S": first}) == {"S": None}  # centroid sits between them

    assert cache.unfold("Albert", "job-b", "SPEAKER_01") is True

    assert cache.resolve_embeddings({"S": first}) == {"S": "Albert"}
    assert [member.ref for member in cache.members("Albert")] == [("job-a", "SPEAKER_00")]


def test_unfolding_the_last_member_drops_the_name(tmp_path):
    """A name with no voice behind it matches nothing; keeping it listed only
    invites folding an unrelated speaker into an empty shell."""
    cache = SpeakerCache(tmp_path / "speakers.json", threshold=0.75)
    cache.fold("Albert", [1.0, 0.0], job_id="job-a", speaker_id="SPEAKER_00")

    assert cache.unfold("Albert", "job-a", "SPEAKER_00") is True

    assert cache.names() == []


def test_unfold_reports_a_member_it_does_not_have(tmp_path):
    cache = SpeakerCache(tmp_path / "speakers.json", threshold=0.75)
    cache.fold("Albert", [1.0, 0.0], job_id="job-a", speaker_id="SPEAKER_00")

    assert cache.unfold("Albert", "job-z", "SPEAKER_09") is False
    assert cache.unfold("Nobody", "job-a", "SPEAKER_00") is False


def test_relabeling_the_same_speaker_replaces_its_member(tmp_path):
    """One speaker in one job is one voice however often it is renamed; stacking
    a member per attempt would let it outweigh everyone else in its own name."""
    cache = SpeakerCache(tmp_path / "speakers.json", threshold=0.75)
    cache.fold("Albert", [1.0, 0.0], job_id="job-a", speaker_id="SPEAKER_00")
    cache.fold("Albert", [1.0, 0.0], job_id="job-a", speaker_id="SPEAKER_00")

    assert len(cache.members("Albert")) == 1


def test_merge_names_folds_one_voice_into_another(tmp_path):
    """The same person typed two ways ("Michiel", "michiel") is two centroids
    competing for the same speakers; merging has to leave one that matches both."""
    cache = SpeakerCache(tmp_path / "speakers.json", threshold=0.75)
    left = [1.0, 0.0, 0.0]
    right = [0.9, 0.44, 0.0]
    cache.fold("Michiel", left, job_id="job-a", speaker_id="SPEAKER_00")
    cache.fold("michiel", right, job_id="job-b", speaker_id="SPEAKER_00")

    assert cache.merge_names("michiel", "Michiel") is True

    assert cache.names() == ["Michiel"]
    assert cache.resolve_embeddings({"L": left, "R": right}) == {"L": "Michiel", "R": "Michiel"}
    assert len(cache.members("Michiel")) == 2


def test_legacy_averaged_entry_becomes_one_unplayable_member(tmp_path):
    """Caches written before this layout kept no provenance, so their voices can
    be matched and folded into but never played back or unfolded. They must
    migrate rather than be dropped - they are someone's accumulated labeling."""
    path = tmp_path / "speakers.json"
    path.write_text(
        json.dumps({"speakers": {"Albert": {"embedding": [3.0, 0.0], "count": 4}}}),
        encoding="utf-8",
    )
    cache = SpeakerCache(path, threshold=0.75, namespace=LEGACY_NAMESPACE)

    members = cache.members("Albert")

    assert cache.resolve_embeddings({"S": [1.0, 0.0]}) == {"S": "Albert"}
    assert [member.playable for member in members] == [False]
    assert members[0].weight == 4.0


def test_migrated_weight_keeps_an_established_voice_from_being_hijacked(tmp_path):
    """A name learned from four recordings must not be yanked onto a fifth voice.

    The old layout tracked that history only as `count`; dropping it on migration
    would make every accumulated name as movable as a brand new one.
    """
    path = tmp_path / "speakers.json"
    path.write_text(
        json.dumps({"speakers": {"Albert": {"embedding": [1.0, 0.0], "count": 4}}}),
        encoding="utf-8",
    )
    cache = SpeakerCache(path, threshold=0.75, namespace=LEGACY_NAMESPACE)

    cache.fold("Albert", [0.0, 1.0], job_id="job-new", speaker_id="SPEAKER_00")

    assert cache.resolve_embeddings({"S": [1.0, 0.0]}) == {"S": "Albert"}


def test_suggest_reports_the_score_of_a_match_it_rejects(tmp_path):
    """The UI has to tell "I don't know this voice" from "I half-know it", so a
    below-threshold match still has to come back with its score."""
    cache = SpeakerCache(tmp_path / "speakers.json", threshold=0.75)
    cache.fold("Albert", [1.0, 0.0], job_id="job-a", speaker_id="SPEAKER_00")

    label, score = cache.suggest_embeddings({"S": [0.5, 0.866]})["S"]

    assert label == "Albert"
    assert score == pytest.approx(0.5, abs=0.01)
    assert cache.resolve_embeddings({"S": [0.5, 0.866]}) == {"S": None}


def test_similarity_of_an_unknown_name_is_none(tmp_path):
    cache = SpeakerCache(tmp_path / "speakers.json", threshold=0.75)
    cache.fold("Albert", [1.0, 0.0], job_id="job-a", speaker_id="SPEAKER_00")

    assert cache.similarity("Albert", [1.0, 0.0]) == pytest.approx(1.0)
    assert cache.similarity("Nobody", [1.0, 0.0]) is None
