"""Offline adapter, parser, cache and pipeline contract tests."""

from __future__ import annotations

import ast
import builtins
import json
import logging
import sys
import types
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from incubrix.adapters.base import (
    AdapterConfigurationError,
    AdapterError,
    AdapterTransientError,
    InstructionNotUnderstoodError,
    Usage,
)
from incubrix.adapters.embed.base import cosine_similarity
from incubrix.adapters.embed.factory import get_embedder
from incubrix.adapters.embed.local import SentenceTransformersEmbedder
from incubrix.adapters.embed.mock import MockEmbedder
from incubrix.adapters.llm.base import ParseResult
from incubrix.adapters.llm.cache import MemoryParseCache, parse_cache_key
from incubrix.adapters.llm.factory import get_llm_adapter
from incubrix.adapters.llm.mock import MockLLMAdapter
from incubrix.adapters.llm.openai_compatible import OpenAICompatibleLLMAdapter
from incubrix.adapters.stt.factory import get_stt_adapter
from incubrix.adapters.stt.faster_whisper import FasterWhisperSTTAdapter
from incubrix.adapters.stt.mock import MockSTTAdapter
from incubrix.config import Settings
from incubrix.engine.models import Sentence, Transcript, Word
from incubrix.engine.session import SessionManager
from incubrix.pipeline.cost import estimate_llm_cost
from incubrix.pipeline.instructions import run_instruction
from incubrix.pipeline.intents import IntentOps, intent_json_schema
from tests.unit.conftest import source as source_fixture
from tests.unit.conftest import transcript as transcript_fixture


def _session() -> SessionManager:
    return SessionManager(source_fixture.__wrapped__(), transcript_fixture.__wrapped__())


def _response(ops: list[dict], input_tokens: int = 11, output_tokens: int = 7) -> dict:
    return {
        "choices": [{"message": {"content": json.dumps({"ops": ops})}}],
        "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens},
    }


def test_mock_stt_assigns_stable_ids_flags_low_confidence_and_sentences(tmp_path: Path) -> None:
    transcript_path = tmp_path / "words.json"
    transcript_path.write_text(
        json.dumps(
            {
                "words": [
                    {"text": "hello", "start_s": 0, "end_s": 0.4, "confidence": 0.9},
                    {"text": "world.", "start_s": 0.5, "end_s": 1, "confidence": 0.2},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = MockSTTAdapter(0.6).transcribe(transcript_path)

    assert [word.id for word in result.transcript.words] == ["w0001", "w0002"]
    assert result.transcript.words[1].flags == ["low_confidence"]
    assert result.transcript.sentences == [Sentence(first_word_idx=0, last_word_idx=1)]
    assert result.usage.provider == "mock"
    assert result.usage.model == "prepared-transcript"


def test_mock_stt_respects_explicit_sentences_and_rejects_bad_json_shape(tmp_path: Path) -> None:
    source = tmp_path / "transcript.json"
    source.write_text(
        json.dumps(
            {
                "words": [{"text": "One.", "start_s": 0, "end_s": 0.5}],
                "sentences": [{"first_word_idx": 0, "last_word_idx": 0}],
                "speakers": ["A"],
                "warnings": ["prepared"],
            }
        ),
        encoding="utf-8",
    )
    output = MockSTTAdapter().transcribe(source).transcript
    assert output.sentences == [Sentence(first_word_idx=0, last_word_idx=0)]
    assert output.speakers == ["A"]
    assert output.warnings == ["prepared"]
    source.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="object"):
        MockSTTAdapter().transcribe(source)


def test_stt_normalizer_rejects_invalid_words_and_handles_empty_transcript(tmp_path: Path) -> None:
    source = tmp_path / "transcript.json"
    source.write_text(json.dumps({"words": []}), encoding="utf-8")
    assert MockSTTAdapter().transcribe(source).transcript.sentences == []
    source.write_text(json.dumps({"words": "not a list"}), encoding="utf-8")
    with pytest.raises(ValueError, match="must be a list"):
        MockSTTAdapter().transcribe(source)
    source.write_text(json.dumps({"words": [None]}), encoding="utf-8")
    with pytest.raises(ValueError, match="must be an object"):
        MockSTTAdapter().transcribe(source)
    source.write_text(
        json.dumps({"words": [{"text": "plain", "start_s": 0, "end_s": 0.5}]}),
        encoding="utf-8",
    )
    assert len(MockSTTAdapter().transcribe(source).transcript.sentences) == 1


def test_faster_whisper_adapter_loads_dependency_only_when_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MockWord:
        word = " Hi."
        start = 0.0
        end = 0.4
        probability = 0.45

    class MockSegment:
        words = [MockWord()]

    class MockModel:
        def transcribe(self, path: str, word_timestamps: bool) -> tuple[list, object]:
            assert path == "local.mp4"
            assert word_timestamps is True
            return [MockSegment()], types.SimpleNamespace(duration=1.0)

    class MockWhisperModel:
        def __init__(self, model_name: str, device: str) -> None:
            assert model_name == "tiny"
            assert device == "cpu"

        def transcribe(self, path: str, word_timestamps: bool) -> tuple[list, object]:
            return MockModel().transcribe(path, word_timestamps)

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        types.SimpleNamespace(WhisperModel=MockWhisperModel),
    )
    adapter = FasterWhisperSTTAdapter("tiny", low_confidence_threshold=0.6)
    assert adapter._model is None
    result = adapter.transcribe("local.mp4")
    assert result.transcript.words[0].id == "w0001"
    assert result.transcript.words[0].flags == ["low_confidence"]
    assert result.usage.provider == "faster_whisper"
    assert result.usage.details == {"audio_duration_s": 1.0}


def test_faster_whisper_adapter_reports_missing_optional_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def blocked_import(name: str, *args, **kwargs):
        if name == "faster_whisper":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(AdapterConfigurationError, match="install the 'stt' extra"):
        FasterWhisperSTTAdapter("tiny").transcribe("clip.mp4")


def test_faster_whisper_adapter_rejects_missing_word_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptySegment:
        words = None

    class MockWhisperModel:
        def __init__(self, model_name: str, device: str) -> None:
            pass

        def transcribe(self, path: str, word_timestamps: bool) -> tuple[list, object]:
            return [EmptySegment()], types.SimpleNamespace(duration=0.5)

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        types.SimpleNamespace(WhisperModel=MockWhisperModel),
    )
    with pytest.raises(ValueError, match="without word timestamps"):
        FasterWhisperSTTAdapter("tiny").transcribe("clip.mp4")


def test_mock_embedder_and_similarity_are_deterministic() -> None:
    embedder = MockEmbedder(dimensions=32)
    vectors = embedder.embed(["pricing cost", "pricing cost"])
    assert vectors[0] == vectors[1]
    assert embedder.similarity("pricing", "pricing") == pytest.approx(1.0)
    assert cosine_similarity([], []) == 0.0
    assert cosine_similarity([1.0], [1.0, 2.0]) == 0.0
    assert cosine_similarity([0.0], [1.0]) == 0.0
    with pytest.raises(ValueError, match="positive"):
        MockEmbedder(dimensions=0)


def test_local_embedder_loads_lazily_and_reports_missing_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeModel:
        def encode(self, texts: list[str], convert_to_numpy: bool) -> list[list[float]]:
            assert convert_to_numpy is True
            return [[1.0, 0.0] if text == "left" else [0.0, 1.0] for text in texts]

    class FakeSentenceTransformer:
        def __init__(self, name: str) -> None:
            assert name == "test-model"

        def encode(self, texts: list[str], convert_to_numpy: bool) -> list[list[float]]:
            return FakeModel().encode(texts, convert_to_numpy)

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=FakeSentenceTransformer),
    )
    embedder = SentenceTransformersEmbedder("test-model")
    assert embedder._model is None
    assert embedder.embed(["left", "right"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert embedder.similarity("left", "right") == pytest.approx(0.0)

    real_import = builtins.__import__

    def blocked_import(name: str, *args, **kwargs):
        if name == "sentence_transformers":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    with pytest.raises(AdapterConfigurationError, match="install the 'embed' extra"):
        SentenceTransformersEmbedder("missing").embed(["text"])


def test_adapter_factories_select_configured_providers_or_fail_clearly() -> None:
    defaults = Settings(_env_file=None)
    assert isinstance(get_stt_adapter(defaults), MockSTTAdapter)
    assert isinstance(get_embedder(defaults), MockEmbedder)
    assert isinstance(get_llm_adapter(defaults), MockLLMAdapter)
    assert isinstance(
        get_stt_adapter(Settings(_env_file=None, STT_PROVIDER="faster_whisper")),
        FasterWhisperSTTAdapter,
    )
    assert isinstance(
        get_embedder(Settings(_env_file=None, EMBEDDER_PROVIDER="local")),
        SentenceTransformersEmbedder,
    )

    groq = Settings(_env_file=None, LLM_PROVIDER="groq", GROQ_API_KEY="test")
    openai = Settings(_env_file=None, LLM_PROVIDER="openai", OPENAI_API_KEY="test")
    common_key = Settings(_env_file=None, LLM_PROVIDER="groq", LLM_API_KEY="common")
    assert get_llm_adapter(groq).provider == "groq"
    assert get_llm_adapter(openai).provider == "openai"
    assert get_llm_adapter(common_key)._api_key == "common"
    with pytest.raises(AdapterConfigurationError, match="GROQ_API_KEY"):
        get_llm_adapter(Settings(_env_file=None, LLM_PROVIDER="groq"))
    with pytest.raises(AdapterConfigurationError, match="Unsupported STT"):
        get_stt_adapter(Settings(_env_file=None, STT_PROVIDER="unknown"))
    with pytest.raises(AdapterConfigurationError, match="Unsupported embedder"):
        get_embedder(Settings(_env_file=None, EMBEDDER_PROVIDER="unknown"))
    with pytest.raises(AdapterConfigurationError, match="Unsupported LLM"):
        get_llm_adapter(Settings(_env_file=None, LLM_PROVIDER="unknown"))


def test_intent_schema_is_strict_and_covers_every_engine_intent() -> None:
    op_names = {
        "cut",
        "keep_only",
        "speed",
        "reorder",
        "add_caption",
        "style",
        "mute",
        "bleep",
        "restore",
        "remove_fillers",
        "remove_silence",
        "keep_pause",
        "auto_caption",
        "add_music",
        "undo",
        "redo",
        "render",
    }
    schema = IntentOps.model_json_schema()
    operation_mapping = schema["properties"]["ops"]["items"]["discriminator"]["mapping"]
    assert set(operation_mapping) == op_names
    with pytest.raises(ValidationError):
        IntentOps.model_validate({"ops": [{"op": "cut", "target": {"kind": "unknown"}}]})
    with pytest.raises(ValidationError):
        IntentOps.model_validate({"ops": [{"op": "cut", "target": {"kind": "time", "start": 1}}]})
    with pytest.raises(ValidationError):
        IntentOps.model_validate({"ops": [{"op": "cut", "target": {"kind": "time", "end": 1}}]})
    with pytest.raises(ValidationError):
        IntentOps.model_validate(
            {
                "ops": [
                    {
                        "op": "cut",
                        "target": {"kind": "time", "from_start": 1, "from_end": 1},
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        IntentOps.model_validate(
            {
                "ops": [
                    {
                        "op": "cut",
                        "target": {"kind": "time", "start": 1, "end": 2, "from_start": 3},
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        IntentOps.model_validate(
            {
                "ops": [
                    {
                        "op": "cut",
                        "target": {"kind": "time", "start": 1, "from_start": 2},
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        IntentOps.model_validate({"ops": [{"op": "undo", "unexpected": True}]})
    with pytest.raises(ValidationError):
        IntentOps.model_validate({"ops": [{"op": "cut", "target": {"kind": "caption"}}]})
    with pytest.raises(ValidationError):
        IntentOps.model_validate(
            {
                "ops": [
                    {
                        "op": "speed",
                        "factor": "2",
                        "target": {"kind": "time", "start": 0, "end": 1},
                    }
                ]
            }
        )
    assert IntentOps.model_validate({"ops": [{"op": "remove_silence", "min_gap_s": 0}]}).ops
    assert IntentOps.model_validate(
        {"ops": [{"op": "add_music", "track_id": "track", "replace": True}]}
    ).ops


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Add a caption 'Welcome!' when I say welcome to the channel", "add_caption"),
        ("Cut the first 30 seconds", "cut"),
        ("Remove the part about pricing", "cut"),
        ("Speed up from 1:00 to 1:30 by 2x", "speed"),
        ("Speed up 10x", "speed"),
        ("Actually, put the intro back", "restore"),
        ("Undo", "undo"),
        ("Redo", "redo"),
        ("Make that caption red", "style"),
        ("Remove 0:30-0:40", "cut"),
        ("Cut between 80 seconds and 85 seconds", "cut"),
        ("Cut the first a minute twenty", "cut"),
        ("Cut from a minute and twenty seconds to a minute thirty", "cut"),
        ("Cut the last 10 seconds", "cut"),
        ("Cut the phrase 'thanks for watching'", "cut"),
        ('Cut "thanks for watching"', "cut"),
        ("Remove everything about cost", "cut"),
        ("remove everything after I say thanks", "cut"),
        ("remove the ums", "remove_fillers"),
        ("remove silences longer than 2 seconds", "remove_silence"),
        ("add captions to the whole video", "auto_caption"),
        ("add background music", "add_music"),
        ("mute the word pricing", "mute"),
        ("bleep pricing", "bleep"),
        ("keep that pause", "keep_pause"),
        ("undo the second-to-last cut", "restore"),
        ("make it bigger", "style"),
        ("render", "render"),
    ],
)
def test_mock_parser_covers_required_phrasings(text: str, expected: str) -> None:
    result = MockLLMAdapter().parse(text, {"video_duration_s": 300})
    assert result.ops[0]["op"] == expected


def test_mock_parser_rejects_unsupported_instructions() -> None:
    with pytest.raises(InstructionNotUnderstoodError):
        MockLLMAdapter().parse("make something cinematic", {})


def test_mock_parser_preserves_explicit_original_time_policy() -> None:
    result = MockLLMAdapter().parse("remove 0:30-0:40 in original time", {})
    assert result.ops[0]["target"] == {
        "kind": "time",
        "start": 30.0,
        "end": 40.0,
        "policy": "original",
    }
    last = MockLLMAdapter().parse("cut the last 10 seconds in original time", {})
    assert last.ops[0]["target"]["policy"] == "original"


def test_mock_parser_parses_compound_minute_durations_without_truncation() -> None:
    parser = MockLLMAdapter()
    first = parser.parse("Cut the first a minute twenty", {})
    assert first.ops[0]["target"]["from_start"] == 80
    ranged = parser.parse("Cut from a minute and twenty seconds to a minute thirty", {})
    assert ranged.ops[0]["target"]["start"] == 80
    assert ranged.ops[0]["target"]["end"] == 90


def test_mock_parser_rejects_unparseable_time_and_silence_threshold() -> None:
    parser = MockLLMAdapter()
    with pytest.raises(InstructionNotUnderstoodError):
        parser.parse("speed up from bananas to snacks by 2x", {})
    with pytest.raises(InstructionNotUnderstoodError):
        parser.parse("remove silences longer than forever", {})


def test_mock_parser_translates_whole_video_cut_and_rejects_bad_internal_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert MockLLMAdapter().parse("cut everything", {"video_duration_s": 12}).ops == [
        {"op": "cut", "target": {"kind": "time", "start": 0.0, "end": 12.0, "policy": "current"}}
    ]
    topic = MockLLMAdapter().parse("Remove everything about cost", {})
    assert topic.ops[0]["target"] == {"kind": "topic", "query": "cost"}

    def invalid_model(*args, **kwargs):
        raise ValueError("invalid")

    monkeypatch.setattr(IntentOps, "model_validate", invalid_model)
    with pytest.raises(InstructionNotUnderstoodError):
        MockLLMAdapter().parse("Undo", {})


def test_parse_cache_is_defensive_and_cache_key_normalizes_instruction() -> None:
    cache = MemoryParseCache()
    ops = [{"op": "undo"}]
    cache.set("key", ops)
    ops[0]["op"] = "redo"
    read = cache.get("key")
    assert read == [{"op": "undo"}]
    read[0]["op"] = "render"
    assert cache.get("key") == [{"op": "undo"}]
    key = parse_cache_key("mock", "m", "v1", "  CUT   this ", {"duration": 10})
    assert key == parse_cache_key("mock", "m", "v1", "cut this", {"duration": 10})
    assert key != parse_cache_key("mock", "m", "v1", "cut this", {"duration": 11})


def test_openai_compatible_success_uses_json_mode_temperature_zero_and_usage() -> None:
    observed: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers["Authorization"]
        observed["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_response([{"op": "undo"}]))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleLLMAdapter(
        model="test",
        base_url="https://llm.invalid/v1/",
        api_key="hidden-key",
        provider="groq",
        client=client,
    )
    result = adapter.parse(
        "Undo",
        {
            "known_caption_texts": ["Welcome!"],
            "active_edit_count": 2,
            "video_duration_s": 250,
            "transcript": "must not be sent",
        },
    )
    assert observed["authorization"] == "Bearer hidden-key"
    assert observed["payload"]["temperature"] == 0
    assert observed["payload"]["seed"] == 0
    assert observed["payload"]["response_format"] == {"type": "json_object"}
    request_text = json.dumps(observed["payload"]["messages"])
    assert "must not be sent" not in request_text
    assert result.ops == [{"op": "undo"}]
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 7


def test_openai_compatible_closes_only_its_owned_http_client() -> None:
    owned = OpenAICompatibleLLMAdapter(
        model="test", base_url="https://llm.invalid/v1", api_key="secret"
    )
    owned_client = owned._client
    owned.close()
    assert owned_client.is_closed

    external_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    )
    external = OpenAICompatibleLLMAdapter(
        model="test",
        base_url="https://llm.invalid/v1",
        api_key="secret",
        client=external_client,
    )
    external.close()
    assert not external_client.is_closed
    external_client.close()
    with pytest.raises(ValueError, match="timeout"):
        OpenAICompatibleLLMAdapter(
            model="test", base_url="https://llm.invalid/v1", api_key="secret", timeout_s=0
        )
    with pytest.raises(ValueError, match="retries"):
        OpenAICompatibleLLMAdapter(
            model="test", base_url="https://llm.invalid/v1", api_key="secret", max_retries=-1
        )


def test_openai_compatible_repairs_invalid_json_once() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        body = (
            {
                "choices": [{"message": {"content": "not json"}}],
                "usage": {"prompt_tokens": 14, "completion_tokens": 4},
            }
            if attempts == 1
            else _response([{"op": "redo"}], 4, 3)
        )
        return httpx.Response(200, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleLLMAdapter(
        model="test", base_url="https://llm.invalid/v1", api_key="secret", client=client
    )
    result = adapter.parse("Redo", {})
    assert attempts == 2
    assert result.ops == [{"op": "redo"}]
    assert result.usage.tokens == 25


def test_openai_compatible_rejects_invalid_json_after_repair() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choices": [{"message": {"content": "not json"}}]}
            )
        )
    )
    adapter = OpenAICompatibleLLMAdapter(
        model="test", base_url="https://llm.invalid/v1", api_key="secret", client=client
    )
    with pytest.raises(InstructionNotUnderstoodError):
        adapter.parse("Redo", {})


def test_openai_compatible_retries_429_and_timeout_without_leaking_key() -> None:
    attempts = 0

    def busy_then_success(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429)
        return httpx.Response(200, json=_response([{"op": "undo"}]))

    client = httpx.Client(transport=httpx.MockTransport(busy_then_success))
    adapter = OpenAICompatibleLLMAdapter(
        model="test",
        base_url="https://llm.invalid/v1",
        api_key="secret-value",
        max_retries=1,
        backoff_s=0,
        client=client,
    )
    assert adapter.parse("Undo", {}).ops == [{"op": "undo"}]
    assert attempts == 2

    timeout_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("timeout"))
        )
    )
    timeout_adapter = OpenAICompatibleLLMAdapter(
        model="test",
        base_url="https://llm.invalid/v1",
        api_key="secret-value",
        max_retries=1,
        backoff_s=0,
        client=timeout_client,
    )
    with pytest.raises(AdapterTransientError) as caught:
        timeout_adapter.parse("Undo", {})
    assert "secret-value" not in str(caught.value)


def test_openai_compatible_does_not_echo_key_for_http_error() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(401, text="unauthorized"))
    )
    adapter = OpenAICompatibleLLMAdapter(
        model="test",
        base_url="https://llm.invalid/v1",
        api_key="secret-value",
        client=client,
    )
    with pytest.raises(AdapterError) as caught:
        adapter.parse("Undo", {})
    assert "secret-value" not in str(caught.value)


def test_openai_compatible_never_logs_or_raises_api_key(caplog: pytest.LogCaptureFixture) -> None:
    secret = "do-not-log-token"
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(500, text=f"failed {secret}"))
    )
    adapter = OpenAICompatibleLLMAdapter(
        model="test",
        base_url="https://llm.invalid/v1",
        api_key=secret,
        max_retries=0,
        client=client,
    )
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(AdapterTransientError) as caught:
            adapter.parse("Undo", {})
    assert secret not in str(caught.value)
    assert secret not in caplog.text


def test_openai_compatible_handles_retry_exhaustion_and_invalid_envelopes() -> None:
    busy_client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    busy = OpenAICompatibleLLMAdapter(
        model="test",
        base_url="https://llm.invalid/v1",
        api_key="secret",
        max_retries=0,
        client=busy_client,
    )
    with pytest.raises(AdapterTransientError) as caught:
        busy.parse("Undo", {})
    assert caught.value.retryable is True
    assert "secret" not in str(caught.value)

    invalid_json_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"no json"))
    )
    invalid_json = OpenAICompatibleLLMAdapter(
        model="test",
        base_url="https://llm.invalid/v1",
        api_key="secret",
        client=invalid_json_client,
    )
    with pytest.raises(InstructionNotUnderstoodError, match="envelope"):
        invalid_json.parse("Undo", {})

    invalid_shape_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
    )
    invalid_shape = OpenAICompatibleLLMAdapter(
        model="test",
        base_url="https://llm.invalid/v1",
        api_key="secret",
        client=invalid_shape_client,
    )
    with pytest.raises(InstructionNotUnderstoodError, match="envelope"):
        invalid_shape.parse("Undo", {})

    transport_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline"))
        )
    )
    transport = OpenAICompatibleLLMAdapter(
        model="test",
        base_url="https://llm.invalid/v1",
        api_key="secret",
        max_retries=0,
        client=transport_client,
    )
    with pytest.raises(AdapterTransientError, match="unavailable"):
        transport.parse("Undo", {})

    transport_attempts = 0

    def retry_transport(request: httpx.Request) -> httpx.Response:
        nonlocal transport_attempts
        transport_attempts += 1
        if transport_attempts == 1:
            raise httpx.ConnectError("offline")
        return httpx.Response(200, json=_response([{"op": "undo"}]))

    retry_client = httpx.Client(transport=httpx.MockTransport(retry_transport))
    retry_adapter = OpenAICompatibleLLMAdapter(
        model="test",
        base_url="https://llm.invalid/v1",
        api_key="secret",
        max_retries=1,
        backoff_s=0,
        client=retry_client,
    )
    assert retry_adapter.parse("Undo", {}).ops == [{"op": "undo"}]
    assert transport_attempts == 2


def test_pipeline_runs_worked_example_in_natural_language_and_uses_answer_without_parse() -> None:
    class CountingMock(MockLLMAdapter):
        calls = 0

        def parse(self, instruction: str, context: dict) -> ParseResult:
            self.calls += 1
            return super().parse(instruction, context)

    session = _session()
    parser = CountingMock()
    embedder = MockEmbedder()
    cache = MemoryParseCache()
    instructions = [
        "Add a caption 'Welcome!' when I say welcome to the channel",
        "Cut the first 30 seconds",
        "Remove the part about pricing",
    ]
    first = run_instruction(session, instructions[0], llm=parser, embedder=embedder, cache=cache)
    assert first["status"] == "applied"
    assert session.get_timeline().overlays[0].anchor.start_word_id == "w0085"
    second = run_instruction(session, instructions[1], llm=parser, embedder=embedder, cache=cache)
    assert second["status"] == "applied"
    assert second["duration_s"] == pytest.approx(270)
    question = run_instruction(session, instructions[2], llm=parser, embedder=embedder, cache=cache)
    assert question["status"] == "needs_clarification"
    assert len(question["options"]) == 4
    parsed_before_answer = parser.calls
    third = run_instruction(
        session,
        "the second one",
        answer="the second one",
        llm=parser,
        embedder=embedder,
        cache=cache,
    )
    assert third["status"] == "applied"
    assert third["duration_s"] == pytest.approx(265)
    assert parser.calls == parsed_before_answer
    fourth = run_instruction(
        session, "Speed up from 1:00 to 1:30 by 2x", llm=parser, embedder=embedder, cache=cache
    )
    fifth = run_instruction(
        session, "Actually, put the intro back", llm=parser, embedder=embedder, cache=cache
    )
    sixth = run_instruction(session, "Undo", llm=parser, embedder=embedder, cache=cache)
    assert [fourth["duration_s"], fifth["duration_s"], sixth["duration_s"]] == pytest.approx(
        [250, 280, 250]
    )
    styled = run_instruction(
        session, "Make that caption red", llm=parser, embedder=embedder, cache=cache
    )
    assert styled["status"] == "applied"
    rendered = run_instruction(session, "Render", llm=parser, embedder=embedder, cache=cache)
    assert rendered["status"] == "applied"
    assert "No video was rendered" in rendered["warnings"][0]
    assert first["timeline_hash"] != second["timeline_hash"]
    assert all(
        "usage" in result for result in (first, second, question, third, fourth, fifth, sixth)
    )


def test_pipeline_routes_invalid_speed_and_applies_valid_multi_part_edits() -> None:
    invalid_speed = run_instruction(
        _session(),
        "Speed up 10x",
        llm=MockLLMAdapter(),
        embedder=MockEmbedder(),
        cache=MemoryParseCache(),
    )
    assert invalid_speed["status"] == "rejected"
    assert invalid_speed["error"]["code"] == "INVALID_SPEED"

    session = _session()
    multi = run_instruction(
        session,
        "Cut 0:30-0:40 and speed up 10x",
        llm=MockLLMAdapter(),
        embedder=MockEmbedder(),
        cache=MemoryParseCache(),
    )
    assert multi["status"] == "applied"
    assert multi["duration_s"] == pytest.approx(290)
    assert any("INVALID_SPEED" in warning for warning in multi["warnings"])
    assert len(session.state.stack) == 1


def test_pipeline_rejects_unparseable_instruction_and_cache_is_deterministic() -> None:
    class CountingMock(MockLLMAdapter):
        calls = 0

        def parse(self, instruction: str, context: dict) -> ParseResult:
            self.calls += 1
            return super().parse(instruction, context)

    parser = CountingMock()
    cache = MemoryParseCache()
    session = _session()
    rejected = run_instruction(
        session, "invent a cinematic montage", llm=parser, cache=cache, embedder=MockEmbedder()
    )
    assert rejected["status"] == "rejected"
    assert rejected["error"]["code"] == "INSTRUCTION_NOT_UNDERSTOOD"

    first_session = _session()
    second_session = _session()
    first = run_instruction(
        first_session, "Cut the first 30 seconds", llm=parser, cache=cache, embedder=MockEmbedder()
    )
    calls_after_first = parser.calls
    second = run_instruction(
        second_session,
        " cut   the first 30 seconds ",
        llm=parser,
        cache=cache,
        embedder=MockEmbedder(),
    )
    assert parser.calls == calls_after_first
    assert second["usage"]["cache_hit"] is True
    assert first["timeline_hash"] == second["timeline_hash"]
    repeated = run_instruction(
        first_session,
        "Cut the first 30 seconds",
        llm=parser,
        cache=cache,
        embedder=MockEmbedder(),
    )
    assert repeated["status"] == "needs_clarification"


def test_pipeline_has_three_run_hash_determinism() -> None:
    hashes = []
    for _ in range(3):
        session = _session()
        parser = MockLLMAdapter()
        embedder = MockEmbedder()
        cache = MemoryParseCache()
        run_hashes = []
        for text in [
            "Add a caption 'Welcome!' when I say welcome to the channel",
            "Cut the first 30 seconds",
            "Remove the part about pricing",
            "the second one",
            "Speed up from 1:00 to 1:30 by 2x",
            "Actually, put the intro back",
            "Undo",
            "Make that caption red",
            "Render",
        ]:
            result = run_instruction(
                session,
                text,
                llm=parser,
                embedder=embedder,
                cache=cache,
            )
            run_hashes.append(result["timeline_hash"])
        hashes.append(run_hashes)
    assert hashes[0] == hashes[1] == hashes[2]


def test_compact_context_digest_is_deterministic() -> None:
    from incubrix.pipeline.instructions import compact_context_digest

    assert compact_context_digest({"b": 2, "a": 1}) == compact_context_digest({"a": 1, "b": 2})


def test_cost_estimate_uses_configured_input_and_output_prices() -> None:
    settings = Settings(
        _env_file=None,
        LLM_INPUT_COST_PER_1K_TOKENS=0.002,
        LLM_OUTPUT_COST_PER_1K_TOKENS=0.004,
    )
    assert estimate_llm_cost(
        Usage(provider="test", model="test", input_tokens=1000, output_tokens=500), settings
    ) == pytest.approx(0.004)
    assert estimate_llm_cost(Usage(provider="mock", model="mock")) == 0.0
    legacy_settings = Settings(_env_file=None, LLM_COST_PER_1K_TOKENS=0.001)
    assert estimate_llm_cost(
        Usage(provider="legacy", model="legacy", input_tokens=100, output_tokens=100),
        legacy_settings,
    ) == pytest.approx(0.0002)


def test_engine_import_graph_does_not_depend_on_adapters_or_pipeline() -> None:
    engine_root = Path(__file__).parents[2] / "incubrix" / "engine"
    for source_path in engine_root.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
        assert all(
            not module.startswith(("incubrix.adapters", "incubrix.pipeline")) for module in modules
        )


def test_settings_default_adapters_remain_offline_mocks() -> None:
    settings = Settings(_env_file=None)
    assert (settings.STT_PROVIDER, settings.LLM_PROVIDER, settings.EMBEDDER_PROVIDER) == (
        "mock",
        "mock",
        "mock",
    )
    for invalid in (
        {"LLM_TIMEOUT_S": 0},
        {"LLM_MAX_RETRIES": -1},
        {"EMBEDDER_DIMENSIONS": 0},
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **invalid)


def test_pipeline_rejects_invalid_ops_from_an_adapter() -> None:
    class InvalidAdapter:
        provider = "test"
        model = "invalid"

        def parse(self, instruction: str, context: dict) -> ParseResult:
            return ParseResult(
                ops=[{"op": "cut", "target": {"kind": "time", "start": 2}}],
                usage=Usage(provider="test", model="invalid"),
            )

    result = run_instruction(
        _session(),
        "anything",
        llm=InvalidAdapter(),
        embedder=MockEmbedder(),
        cache=MemoryParseCache(),
    )
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "INSTRUCTION_NOT_UNDERSTOOD"


def test_pipeline_closes_only_owned_llm_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    import incubrix.pipeline.instructions as instructions

    created: list[MockLLMAdapter] = []

    class OwnedMock(MockLLMAdapter):
        closed = False

        def close(self) -> None:
            self.closed = True

    def create_adapter(settings: Settings) -> OwnedMock:
        adapter = OwnedMock()
        created.append(adapter)
        return adapter

    monkeypatch.setattr(instructions, "get_llm_adapter", create_adapter)
    cache = MemoryParseCache()
    for _ in range(2):
        run_instruction(
            _session(),
            "Undo",
            cache=cache,
            embedder=MockEmbedder(),
            settings=Settings(_env_file=None),
        )
    assert len(created) == 2
    assert all(adapter.closed for adapter in created)


def test_transcript_types_are_engine_owned() -> None:
    """Keep the adapter contract pinned to the existing engine datamodel."""
    assert Transcript.__module__ == "incubrix.engine.models"
    assert Word.__module__ == "incubrix.engine.models"


def test_exported_intent_json_schema_matches_models() -> None:
    schema_path = Path(__file__).parents[2] / "schemas" / "intent_ops.schema.json"
    assert json.loads(schema_path.read_text(encoding="utf-8")) == intent_json_schema()


def test_intent_json_schema_omits_explicit_default_object_allowance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        IntentOps,
        "model_json_schema",
        classmethod(lambda cls: {"additionalProperties": True}),
    )
    assert intent_json_schema() == {}
