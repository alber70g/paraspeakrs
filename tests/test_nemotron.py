"""Nemotron backend: requests, response parsing, speaker mapping, config and a pipeline run.

No server and no model: the HTTP layer is httpx's MockTransport, and the response
fixtures follow the documented shapes of both servers.
"""

from __future__ import annotations

import base64
import json
import wave
from pathlib import Path

import httpx
import pytest

from paraspeakrs import cli
from paraspeakrs.cache import SpeakerCache, embedding_namespace
from paraspeakrs.config import NEMOTRON_SPEAKER_OVERLAP_SECONDS, Settings
from paraspeakrs.factory import build_pipeline, describe_asr
from paraspeakrs.merge import merge_asr_speaker_words
from paraspeakrs.models import AsrSegment, Word
from paraspeakrs.nemotron import (
    CLEAN_PROMPT,
    SPEAKER_PROMPT,
    AsrSpeakerDiarizer,
    NemotronTranscribeAsr,
    SpeakerTagMapper,
    build_prompt,
    build_request,
    diarization_from_words,
    normalize_base_url,
    parse_response,
    speaker_tag,
    strip_tags,
)
from paraspeakrs.pipeline import TranscriptionPipeline

# vLLM: the transcript in the message, words under a top-level ctc_timestamps.
VLLM_RESPONSE = {
    "id": "chatcmpl-1",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "<spk:0> Hello there. <spk:1> Hi, Albert.<|im_end|>"},
            "finish_reason": "stop",
        }
    ],
    "ctc_timestamps": {
        "words": [
            {"word": "Hello", "start": 0.2, "end": 0.5, "speaker": 0},
            {"word": "there.", "start": 0.5, "end": 0.9, "speaker": 0},
            {"word": "Hi,", "start": 1.4, "end": 1.6, "speaker": 1},
            {"word": "Albert.", "start": 1.6, "end": 2.1, "speaker": 1},
        ],
        "diarization": [[0.0, 1.0, 0], [1.3, 2.2, 1]],
        "speaker_tag_to_diarization_speaker": {"0": 0, "1": 1},
        "error": None,
    },
}

# NeMo-Speech.cpp: the structured result under `transcription`, speakers as "spk:N".
NEMO_RESPONSE = {
    "object": "chat.completion",
    "model": "salm",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "Hello there. Hi, Albert."}, "finish_reason": "stop"}
    ],
    "transcription": {
        "schema_version": "1",
        "task": "transcribe",
        "language": "und",
        "duration": 2.5,
        "text": "Hello there. Hi, Albert.",
        "segments": [],
        "words": [
            {"word": "Hello", "word_index": 0, "start": 0.2, "end": 0.5, "speaker": "spk:0", "speaker_slot": 0},
            {"word": "there.", "word_index": 1, "start": 0.5, "end": 0.9, "speaker": "spk:0", "speaker_slot": 0},
            {"word": "Hi,", "word_index": 2, "start": 1.4, "end": 1.6, "speaker": "spk:1", "speaker_slot": 1},
            {"word": "Albert.", "word_index": 3, "start": 1.6, "end": 2.1, "speaker": "spk:1", "speaker_slot": 1},
        ],
        "word_timestamp_alignment": {"status": "aligned", "model": "salm", "failure_reason": None},
    },
}


def _wav(path: Path, seconds: float = 1.0) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * int(16000 * seconds))
    return path


def _asr_with_server(response: dict | int, seen: list[httpx.Request], **kwargs) -> NemotronTranscribeAsr:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(response, int):
            return httpx.Response(response, text="boom")
        return httpx.Response(200, json=response)

    return NemotronTranscribeAsr(transport=httpx.MockTransport(handler), **kwargs)


# --- request building ---------------------------------------------------------


def test_nemo_speech_request_has_only_documented_fields():
    body = build_request("nemo-speech", "salm", "PROMPT", b"RIFFdata")

    # NeMo-Speech.cpp answers 400 to any field it does not list, max_tokens included.
    assert set(body) == {"model", "timestamp_granularities", "messages"}
    assert body["model"] == "salm"
    assert body["timestamp_granularities"] == ["word"]
    (message,) = body["messages"]
    text, audio = message["content"]
    assert message["role"] == "user"
    assert text == {"type": "text", "text": "PROMPT"}
    assert audio["type"] == "input_audio"
    assert audio["input_audio"]["format"] == "wav"
    assert base64.b64decode(audio["input_audio"]["data"]) == b"RIFFdata"


def test_vllm_request_asks_for_ctc_timestamps_and_raw_tags():
    body = build_request("vllm", "the-model", "PROMPT", b"RIFFdata")

    assert body["model"] == "the-model"
    assert body["temperature"] == 0
    assert body["max_tokens"] >= 20000
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["skip_special_tokens"] is False
    assert body["spaces_between_special_tokens"] is False
    assert body["mm_processor_kwargs"] == {"capture_ctc_timestamps": True}
    text, audio = body["messages"][0]["content"]
    assert text == {"type": "text", "text": "PROMPT"}
    assert audio["type"] == "audio_url"
    url = audio["audio_url"]["url"]
    assert url.startswith("data:audio/wav;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"RIFFdata"


def test_prompts():
    assert build_prompt(speakers=False) == CLEAN_PROMPT
    assert "omit fillers and accidental repetitions" in CLEAN_PROMPT
    assert "Include all spoken words and verbal fillers" not in SPEAKER_PROMPT
    assert build_prompt(speakers=True) == SPEAKER_PROMPT
    assert all(f"<spk:{n}>" in SPEAKER_PROMPT for n in range(8))
    assert build_prompt(speakers=False, context_terms=" Kubernetes, Albert ") == f"{CLEAN_PROMPT} Context terms: Kubernetes, Albert."
    assert build_prompt(speakers=True, context_terms="  ") == SPEAKER_PROMPT


@pytest.mark.parametrize(
    "given",
    ["http://127.0.0.1:8080", "http://127.0.0.1:8080/", "http://127.0.0.1:8080/v1", "http://127.0.0.1:8080/v1/"],
)
def test_base_url_is_normalized_without_v1(given):
    assert normalize_base_url(given) == "http://127.0.0.1:8080"


def test_transcribe_posts_to_chat_completions_with_the_chunk(tmp_path):
    seen: list[httpx.Request] = []
    asr = _asr_with_server(
        NEMO_RESPONSE, seen, base_url="http://box:8080/v1", server="nemo-speech", context_terms="Albert"
    )
    wav = _wav(tmp_path / "chunk.wav", 2.5)

    segment = asr.transcribe(wav)

    (request,) = seen
    assert str(request.url) == "http://box:8080/v1/chat/completions"
    body = json.loads(request.content)
    assert body["model"] == "salm"
    assert body["messages"][0]["content"][0]["text"].endswith("Context terms: Albert.")
    assert base64.b64decode(body["messages"][0]["content"][1]["input_audio"]["data"]) == wav.read_bytes()
    assert segment.text == "Hello there. Hi, Albert."
    assert segment.start == 0.0
    assert segment.end == pytest.approx(2.5)
    assert [word.word for word in segment.words] == ["Hello", "there.", "Hi,", "Albert."]
    # Plain transcription ignores the speakers the server volunteers.
    assert {word.speaker for word in segment.words} == {"UNKNOWN"}


def test_vllm_default_model_and_speaker_prompt(tmp_path):
    seen: list[httpx.Request] = []
    asr = _asr_with_server(VLLM_RESPONSE, seen, base_url="http://box:8000", server="vllm", speakers=True)

    segment = asr.transcribe(_wav(tmp_path / "chunk.wav"))

    body = json.loads(seen[0].content)
    assert body["model"].startswith("nvidia/")
    assert body["messages"][0]["content"][0]["text"] == SPEAKER_PROMPT
    assert [word.speaker for word in segment.words] == ["spk:0", "spk:0", "spk:1", "spk:1"]


def test_server_error_names_status_and_body(tmp_path):
    asr = _asr_with_server(422, [], base_url="http://box:8080")

    with pytest.raises(RuntimeError, match="422.*boom"):
        asr.transcribe(_wav(tmp_path / "chunk.wav"))


def test_unreachable_server_is_a_readable_error(tmp_path):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    asr = NemotronTranscribeAsr("http://box:8080", transport=httpx.MockTransport(refuse))

    with pytest.raises(RuntimeError, match="cannot reach the Nemotron server at http://box:8080"):
        asr.transcribe(_wav(tmp_path / "chunk.wav"))


# --- response parsing ---------------------------------------------------------


def test_parse_vllm_shape():
    text, words = parse_response(VLLM_RESPONSE, speakers=True)

    assert text == "Hello there. Hi, Albert."
    assert [(w.word, w.start, w.end, w.speaker) for w in words] == [
        ("Hello", 0.2, 0.5, "spk:0"),
        ("there.", 0.5, 0.9, "spk:0"),
        ("Hi,", 1.4, 1.6, "spk:1"),
        ("Albert.", 1.6, 2.1, "spk:1"),
    ]


def test_parse_nemo_speech_shape():
    text, words = parse_response(NEMO_RESPONSE, speakers=True)

    assert text == "Hello there. Hi, Albert."
    assert [w.speaker for w in words] == ["spk:0", "spk:0", "spk:1", "spk:1"]
    assert words[0].start == 0.2 and words[-1].end == 2.1


def test_parse_ignores_speakers_unless_asked():
    _, words = parse_response(NEMO_RESPONSE, speakers=False)

    assert {w.speaker for w in words} == {"UNKNOWN"}


def test_strip_tags_removes_speaker_tags_and_special_tokens():
    assert strip_tags("<spk:0> Hello  there.\n<spk:12> Yes.<|im_end|>") == "Hello there. Yes."
    assert strip_tags("<think></think>Plain text") == "Plain text"
    # Angle brackets that are not tags stay.
    assert strip_tags("a < b and c > d") == "a < b and c > d"


def test_parse_falls_back_to_text_when_timestamps_are_missing():
    unaligned_vllm = {
        "choices": [{"message": {"content": "<spk:0> Just text."}}],
        "ctc_timestamps": {"words": [], "error": "alignment failed"},
    }
    unaligned_nemo = {
        "choices": [{"message": {"content": "Just text."}}],
        "transcription": {
            "text": "Just text.",
            "words": [],
            "word_timestamp_alignment": {"status": "unavailable", "failure_reason": "no adapter"},
        },
    }
    plain = {"choices": [{"message": {"content": "Just text."}}]}

    for payload in (unaligned_vllm, unaligned_nemo, plain):
        assert parse_response(payload, speakers=True) == ("Just text.", [])


def test_parse_takes_text_from_transcription_when_message_is_empty():
    payload = {"choices": [{"message": {"content": ""}}], "transcription": {"text": "From structure.", "words": []}}

    assert parse_response(payload, speakers=False)[0] == "From structure."


def test_parse_skips_malformed_words():
    payload = {
        "choices": [{"message": {"content": "a b"}}],
        "ctc_timestamps": {
            "words": [
                {"word": "b", "start": 1.0, "end": 1.2, "speaker": 0},
                {"word": "a", "start": 0.0, "end": 0.4, "speaker": 0},
                {"word": "", "start": 2.0, "end": 2.1},
                {"word": "no-times"},
                {"word": "null-end", "start": 3.0, "end": None},
                "garbage",
            ]
        },
    }

    _, words = parse_response(payload, speakers=True)

    assert [w.word for w in words] == ["a", "b"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("spk:0", "spk:0"),
        ("spk:3", "spk:3"),
        (2, "spk:2"),
        ("speaker_1", "spk:1"),
        (-1, "UNKNOWN"),
        (None, "UNKNOWN"),
        ("", "UNKNOWN"),
        (True, "UNKNOWN"),
    ],
)
def test_speaker_tag_normalization(raw, expected):
    assert speaker_tag(raw) == expected


# --- speaker mapping across requests ------------------------------------------


def _words(spec: list[tuple[str, float, str]]) -> list[Word]:
    return [Word(word=text, start=start, end=start + 0.4, speaker=speaker) for text, start, speaker in spec]


def test_first_chunk_numbers_speakers_by_first_appearance():
    mapper = SpeakerTagMapper()

    mapped = mapper.map_chunk(_words([("a", 0.0, "spk:3"), ("b", 1.0, "spk:1"), ("c", 2.0, "spk:3")]))

    assert [w.speaker for w in mapped] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]


def test_overlap_vote_maps_swapped_tags_onto_earlier_speakers():
    mapper = SpeakerTagMapper()
    mapper.map_chunk(_words([("a", 10.0, "spk:0"), ("b", 11.0, "spk:0"), ("c", 12.0, "spk:1"), ("d", 13.0, "spk:1")]))

    # The next request hears c and d first, so its tags come out the other way round.
    mapped = mapper.map_chunk(
        _words([("C", 12.1, "spk:0"), ("d", 13.2, "spk:0"), ("e", 14.0, "spk:1"), ("f", 15.0, "spk:0")])
    )

    # spk:0 now is the earlier SPEAKER_01 (c and d are the words both requests heard);
    # spk:1 said nothing in the overlap: a new speaker, not a guess.
    assert [w.speaker for w in mapped] == ["SPEAKER_01", "SPEAKER_01", "SPEAKER_02", "SPEAKER_01"]


def test_each_earlier_speaker_is_taken_once():
    mapper = SpeakerTagMapper()
    mapper.map_chunk(_words([("a", 0.0, "spk:0"), ("b", 1.0, "spk:0"), ("c", 2.0, "spk:0")]))

    # Two tags both overlap the one earlier speaker; the stronger vote keeps it.
    mapped = mapper.map_chunk(_words([("a", 0.0, "spk:0"), ("b", 1.0, "spk:0"), ("c", 2.0, "spk:1")]))

    assert [w.speaker for w in mapped] == ["SPEAKER_00", "SPEAKER_00", "SPEAKER_01"]


def test_words_only_match_when_text_and_time_agree():
    mapper = SpeakerTagMapper(tolerance_seconds=1.0)
    mapper.map_chunk(_words([("the", 5.0, "spk:0")]))

    wrong_time = mapper.map_chunk(_words([("the", 9.0, "spk:0")]))
    assert wrong_time[0].speaker == "SPEAKER_01"

    mapper = SpeakerTagMapper(tolerance_seconds=1.0)
    mapper.map_chunk(_words([("the", 5.0, "spk:0")]))
    wrong_word = mapper.map_chunk(_words([("a", 5.0, "spk:0")]))
    assert wrong_word[0].speaker == "SPEAKER_01"


def test_unknown_words_stay_unknown_and_empty_chunks_keep_the_memory():
    mapper = SpeakerTagMapper()
    mapper.map_chunk(_words([("a", 0.0, "spk:0")]))

    assert mapper.map_chunk([]) == []
    mapped = mapper.map_chunk(_words([("a", 0.0, "spk:5"), ("x", 3.0, "UNKNOWN")]))

    assert [w.speaker for w in mapped] == ["SPEAKER_00", "UNKNOWN"]


# --- diarization from words ---------------------------------------------------


def test_diarization_segments_are_runs_of_same_speaker_words():
    words = [
        Word(word="a", start=0.0, end=0.5, speaker="SPEAKER_00"),
        Word(word="b", start=0.6, end=1.0, speaker="SPEAKER_00"),
        Word(word="c", start=1.2, end=1.6, speaker="SPEAKER_01"),
        Word(word="d", start=1.7, end=2.0, speaker="UNKNOWN"),
        Word(word="e", start=2.1, end=2.5, speaker="SPEAKER_01"),
        Word(word="f", start=9.0, end=9.5, speaker="SPEAKER_01"),
    ]

    segments = diarization_from_words([AsrSegment(text="", start=0.0, end=10.0, words=words)])

    assert [(s.speaker, s.start, s.end) for s in segments] == [
        ("SPEAKER_00", 0.0, 1.0),
        ("SPEAKER_01", 1.2, 1.6),
        ("SPEAKER_01", 2.1, 2.5),  # split by the unknown word
        ("SPEAKER_01", 9.0, 9.5),  # and by a long pause
    ]


def test_merge_keeps_the_speakers_words_came_with():
    words = [
        Word(word="a", start=0.0, end=0.5, speaker="SPEAKER_01"),
        Word(word="b", start=0.6, end=1.0, speaker="SPEAKER_01"),
        Word(word="c", start=1.2, end=1.6, speaker="SPEAKER_00"),
    ]
    textonly = AsrSegment(text="lonely text", start=5.0, end=6.0)

    segments, warnings = merge_asr_speaker_words([AsrSegment(text="a b c", start=0.0, end=2.0, words=words), textonly])

    assert [(s.speaker, s.text) for s in segments] == [
        ("SPEAKER_01", "a b"),
        ("SPEAKER_00", "c"),
        ("UNKNOWN", "lonely text"),
    ]
    assert len(warnings) == 1 and "word timestamps" in warnings[0]


# --- config and CLI -----------------------------------------------------------


def test_nemotron_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("PARAKEET_ASR_BACKEND", "nemotron")
    monkeypatch.setenv("PARAKEET_NEMOTRON_SERVER", "vllm")
    monkeypatch.setenv("PARAKEET_NEMOTRON_BASE_URL", "http://gpu:8000")
    monkeypatch.setenv("PARAKEET_NEMOTRON_MODEL", "custom")
    monkeypatch.setenv("PARAKEET_NEMOTRON_MAX_REQUEST_SECONDS", "3600")
    monkeypatch.setenv("PARAKEET_NEMOTRON_CONTEXT_TERMS", "Albert, Kubernetes")

    settings = Settings.from_env()

    assert settings.asr_backend == "nemotron"
    assert (settings.nemotron_server, settings.nemotron_base_url, settings.nemotron_model) == ("vllm", "http://gpu:8000", "custom")
    assert settings.nemotron_max_request_seconds == 3600.0
    assert settings.nemotron_context_terms == "Albert, Kubernetes"


def test_nemotron_defaults(monkeypatch):
    for name in (
        "PARAKEET_NEMOTRON_SERVER",
        "PARAKEET_NEMOTRON_BASE_URL",
        "PARAKEET_NEMOTRON_MODEL",
        "PARAKEET_NEMOTRON_MAX_REQUEST_SECONDS",
        "PARAKEET_NEMOTRON_CONTEXT_TERMS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()

    assert settings.nemotron_server == "nemo-speech"
    assert settings.nemotron_base_url == "http://127.0.0.1:8080"
    assert settings.nemotron_model is None
    assert settings.nemotron_max_request_seconds == 1500.0
    assert settings.nemotron_context_terms is None


def test_from_env_rejects_bad_nemotron_values(monkeypatch):
    monkeypatch.setenv("PARAKEET_NEMOTRON_SERVER", "triton")
    with pytest.raises(ValueError, match="PARAKEET_NEMOTRON_SERVER"):
        Settings.from_env()

    monkeypatch.setenv("PARAKEET_NEMOTRON_SERVER", "vllm")
    monkeypatch.setenv("PARAKEET_NEMOTRON_MAX_REQUEST_SECONDS", "soon")
    with pytest.raises(ValueError, match="PARAKEET_NEMOTRON_MAX_REQUEST_SECONDS"):
        Settings.from_env()


def test_nemotron_asr_needs_no_sherpa_model_or_speakrs_binary():
    Settings(asr_backend="nemotron", diar_backend="senko", sherpa_model_dir=None).validate_runtime()


def test_nemotron_asr_validates_its_request_length():
    with pytest.raises(ValueError, match="nemotron_max_request_seconds"):
        Settings(asr_backend="nemotron", diar_backend="senko", nemotron_max_request_seconds=0).validate_runtime()


def test_nemotron_speakers_need_the_nemotron_asr_backend():
    settings = Settings(asr_backend="mock", diar_backend="nemotron")

    with pytest.raises(ValueError, match="asr_backend 'nemotron'"):
        settings.validate_runtime()


def test_nemotron_speakers_need_no_speakrs_binary(tmp_path):
    settings = Settings(asr_backend="nemotron", diar_backend="nemotron", speakrs_bin=tmp_path / "absent")

    settings.validate_runtime()


def test_nemotron_speakers_cannot_diarize_without_asr():
    settings = Settings(asr_backend="nemotron", diar_backend="nemotron")

    with pytest.raises(ValueError, match="without running ASR"):
        settings.validate_runtime(require_asr=False)


def test_nemotron_speaker_requests_must_be_longer_than_their_overlap():
    settings = Settings(
        asr_backend="nemotron",
        diar_backend="nemotron",
        nemotron_max_request_seconds=2 * NEMOTRON_SPEAKER_OVERLAP_SECONDS,
    )

    with pytest.raises(ValueError, match="overlap"):
        settings.validate_runtime()


def test_cli_flags_reach_the_settings(monkeypatch):
    captured: list[Settings] = []

    def stop(settings, **kwargs):
        captured.append(settings)
        raise RuntimeError("stop")

    monkeypatch.setattr(cli, "build_pipeline", stop)
    monkeypatch.setattr(
        cli.sys,
        "argv",
        [
            "paraspeakrs",
            "run",
            "meeting.wav",
            "--asr-backend",
            "nemotron",
            "--diar-backend",
            "nemotron",
            "--nemotron-server",
            "vllm",
            "--nemotron-base-url",
            "http://gpu:8000",
            "--nemotron-model",
            "custom",
            "--nemotron-max-request-seconds",
            "3000",
            "--nemotron-context-terms",
            "Albert",
        ],
    )

    with pytest.raises(RuntimeError, match="stop"):
        cli._main()

    (settings,) = captured
    assert (settings.asr_backend, settings.diar_backend) == ("nemotron", "nemotron")
    assert (settings.nemotron_server, settings.nemotron_base_url, settings.nemotron_model) == ("vllm", "http://gpu:8000", "custom")
    assert settings.nemotron_max_request_seconds == 3000.0
    assert settings.nemotron_context_terms == "Albert"


def test_cli_rejects_nemotron_speakers_with_another_asr_backend(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.sys, "argv", ["paraspeakrs", "run", "meeting.wav", "--asr-backend", "mock", "--diar-backend", "nemotron"]
    )

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 2
    assert "asr_backend 'nemotron'" in capsys.readouterr().err


# --- factory ------------------------------------------------------------------


def test_factory_builds_a_plain_nemotron_pipeline(tmp_path):
    settings = Settings(
        asr_backend="nemotron",
        diar_backend="senko",
        workspace_dir=tmp_path,
        nemotron_server="vllm",
        nemotron_base_url="http://gpu:8000/v1",
        nemotron_max_request_seconds=3000.0,
        nemotron_context_terms="Albert",
    )

    pipeline = build_pipeline(settings)

    assert isinstance(pipeline.asr, NemotronTranscribeAsr)
    assert pipeline.asr.speakers is False
    assert pipeline.asr.base_url == "http://gpu:8000"
    assert pipeline.asr.context_terms == "Albert"
    assert pipeline.chunk_target_seconds == 3000.0
    assert pipeline.chunk_overlap_seconds == settings.chunk_overlap_seconds
    assert pipeline.speakers_from_asr is False
    assert "vllm" in describe_asr(pipeline.asr)


def test_factory_builds_the_speaker_mode_pipeline(tmp_path):
    settings = Settings(asr_backend="nemotron", diar_backend="nemotron", workspace_dir=tmp_path)

    pipeline = build_pipeline(settings)

    assert pipeline.asr.speakers is True
    assert pipeline.speakers_from_asr is True
    assert pipeline.chunk_target_seconds == 1500.0
    assert pipeline.chunk_overlap_seconds == NEMOTRON_SPEAKER_OVERLAP_SECONDS
    assert pipeline.speaker_cache.namespace == embedding_namespace("nemotron")
    assert "speaker tags" in describe_asr(pipeline.asr)
    with pytest.raises(RuntimeError, match="no separate diarization pass"):
        pipeline.diarizer.diarize(tmp_path / "x.wav")


def test_other_asr_backends_keep_the_default_chunking(tmp_path):
    pipeline = build_pipeline(Settings(asr_backend="mock", diar_backend="senko", workspace_dir=tmp_path))

    assert (pipeline.chunk_target_seconds, pipeline.chunk_overlap_seconds) == (90.0, 2.0)


# --- a pipeline run in speaker mode -------------------------------------------

DURATION = 400.0


class RecordingAudio:
    def __init__(self) -> None:
        self.last_chunk = (0.0, 0.0)
        self.diar_normalized = False

    def materialize(self, work_dir, *, file_path=None, file_url=None, file_base64=None):
        return Path("raw.wav")

    def normalize_mono_16khz(self, input_path, output_path):
        return Path("normalized.wav")

    def normalize_16khz_preserving_channels(self, input_path, output_path):
        self.diar_normalized = True
        return Path("normalized-diar.wav")

    def duration_seconds(self, input_path):
        return DURATION

    def export_chunk(self, input_path, output_path, start, end):
        self.last_chunk = (start, end)
        return Path(f"chunk-{start}-{end}.wav")


def _true_speaker(second: int) -> str:
    return "B" if (second // 10) % 2 else "A"


class TaggingAsr:
    """One word per second, `w<second>`, spoken by A in even ten-second blocks and by B in odd ones.

    Like the real model it tags speakers per request, numbering them by first
    appearance, so a chunk that opens on B calls B `spk:0`.
    """

    def __init__(self, audio: RecordingAudio) -> None:
        self.audio = audio
        self.requests = 0

    def transcribe(self, audio_path):
        self.requests += 1
        start, end = self.audio.last_chunk
        tags: dict[str, str] = {}
        words = []
        for offset in range(int(end - start)):
            who = _true_speaker(int(start) + offset)
            tag = tags.setdefault(who, f"spk:{len(tags)}")
            words.append(Word(word=f"w{int(start) + offset}", start=float(offset), end=offset + 0.5, speaker=tag))
        return AsrSegment(text=" ".join(w.word for w in words), start=0.0, end=end - start, words=words)


class ExplodingDiarizer:
    name = "must not run"

    def diarize(self, audio_path):
        raise AssertionError("speaker mode must not call a diarizer")


def _speaker_pipeline(tmp_path, audio, asr):
    return TranscriptionPipeline(
        diarizer=ExplodingDiarizer(),
        asr=asr,
        audio=audio,
        speaker_cache=SpeakerCache(tmp_path / "speaker-cache.json", namespace=embedding_namespace("nemotron")),
        chunk_target_seconds=210.0,
        chunk_overlap_seconds=60.0,
        speakers_from_asr=True,
    )


def test_speaker_mode_pipeline_keeps_speakers_consistent_across_requests(tmp_path):
    audio = RecordingAudio()
    asr = TaggingAsr(audio)
    pipeline = _speaker_pipeline(tmp_path, audio, asr)
    progress: list[str] = []

    artifacts = pipeline.run_with_artifacts(
        work_dir=tmp_path, file_path=Path("in.wav"), progress=lambda step, detail, percent: progress.append(step)
    )

    assert asr.requests == 3  # 0-210, 150-360, 300-400: tags flip between consecutive requests
    assert not audio.diar_normalized
    assert "diarization" not in progress
    result = artifacts.result
    words = [word for segment in result.segments for word in segment.words]
    assert [word.word for word in words] == [f"w{n}" for n in range(400)]
    # A speaks first, so A is SPEAKER_00 and B SPEAKER_01 - in every request's output.
    assert [word.speaker for word in words] == [
        "SPEAKER_00" if _true_speaker(n) == "A" else "SPEAKER_01" for n in range(400)
    ]
    assert result.num_speakers == 2
    assert result.warnings == []
    assert [segment.speaker for segment in result.segments[:3]] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]
    assert result.segments[0].text == " ".join(f"w{n}" for n in range(10))
    # Diarization comes from the words, in the usual SPEAKER_NN format.
    assert {segment.speaker for segment in artifacts.diarization} == {"SPEAKER_00", "SPEAKER_01"}
    assert len(artifacts.diarization) == 40
    assert artifacts.diarization[0].start == 0.0 and artifacts.diarization[0].end == 9.5
    # No voice prints, no cross-recording names; the speaker cache must still cope.
    assert artifacts.speaker_embeddings == {}
    assert {word.resolved_label for word in words} == {None}


def test_speaker_mode_refuses_to_skip_asr(tmp_path):
    audio = RecordingAudio()
    pipeline = _speaker_pipeline(tmp_path, audio, TaggingAsr(audio))

    with pytest.raises(ValueError, match="cannot skip ASR"):
        pipeline.run_with_artifacts(work_dir=tmp_path, file_path=Path("in.wav"), transcribe=False)


def test_speaker_mode_placeholder_diarizer_fails_where_a_pass_is_needed(tmp_path):
    with pytest.raises(RuntimeError, match="diar-backend nemotron"):
        AsrSpeakerDiarizer().diarize(tmp_path / "x.wav")
