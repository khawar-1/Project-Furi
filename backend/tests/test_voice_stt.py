"""
Phase 7 Part 1 — the STT model state machine + transcription (fake factory —
no real model, no network, the conftest _hermetic_voice_stt backstop) and the
VoiceConfig coercion matrix.
"""
import pytest

from app.core import voice_stt
from app.core.app_settings import (
    VOICE_STT_MODELS,
    _coerce_voice,
    default_voice_config,
)
from app.core.voice_stt import ModelNotReadyError, ensure_model_loaded, transcribe_audio


class FakeInfo:
    language = "en"
    duration = 2.5


class FakeSegment:
    def __init__(self, text: str):
        self.text = text


class FakeModel:
    def __init__(self, name: str):
        self.name = name

    def transcribe(self, audio, **kwargs):
        return iter([FakeSegment(" Hello "), FakeSegment("world. ")]), FakeInfo()


@pytest.fixture(autouse=True)
def fake_factory():
    """A working fake factory that records every construction."""
    calls: list[str] = []

    def factory(name: str) -> FakeModel:
        calls.append(name)
        return FakeModel(name)

    factory.calls = calls
    voice_stt.STT_MODEL_FACTORY = factory
    yield factory


# ------------------------------------------------------------ state machine


async def test_initial_status_is_not_loaded():
    assert voice_stt.stt_status() == {
        "status": "not_loaded",
        "model": None,
        "error": None,
        "device": None,
    }


async def test_ensure_kicks_a_background_load(fake_factory):
    status = await ensure_model_loaded("small")
    assert status["status"] == "loading" and status["model"] == "small"
    await voice_stt.wait_for_load()
    assert voice_stt.stt_status()["status"] == "ready"
    assert fake_factory.calls == ["small"]


async def test_ensure_is_idempotent_when_ready(fake_factory):
    await ensure_model_loaded("small")
    await voice_stt.wait_for_load()
    status = await ensure_model_loaded("small")
    assert status["status"] == "ready"
    assert fake_factory.calls == ["small"]  # not constructed twice


async def test_ensure_is_a_noop_while_loading(fake_factory):
    await ensure_model_loaded("small")
    await ensure_model_loaded("small")
    await voice_stt.wait_for_load()
    assert fake_factory.calls == ["small"]


async def test_load_failure_is_error_and_retryable():
    attempts: list[str] = []

    def flaky(name: str) -> FakeModel:
        attempts.append(name)
        if len(attempts) == 1:
            raise RuntimeError("network dropped mid-download")
        return FakeModel(name)

    voice_stt.STT_MODEL_FACTORY = flaky
    await ensure_model_loaded("small")
    await voice_stt.wait_for_load()
    status = voice_stt.stt_status()
    assert status["status"] == "error"
    assert "network dropped" in status["error"]
    # The recorded flaky-net gotcha: the next kick must retry, never wedge.
    await ensure_model_loaded("small")
    await voice_stt.wait_for_load()
    assert voice_stt.stt_status()["status"] == "ready"
    assert attempts == ["small", "small"]


async def test_model_change_reloads(fake_factory):
    await ensure_model_loaded("small")
    await voice_stt.wait_for_load()
    status = await ensure_model_loaded("base")
    assert status["status"] == "loading" and status["model"] == "base"
    await voice_stt.wait_for_load()
    assert voice_stt.stt_status() == {
        "status": "ready",
        "model": "base",
        "error": None,
        "device": None,  # the test fake factory doesn't set a device
    }
    assert fake_factory.calls == ["small", "base"]


# -------------------------------------------------------------- transcription


async def test_transcribe_joins_segments(fake_factory):
    await ensure_model_loaded("small")
    await voice_stt.wait_for_load()
    result = await transcribe_audio(b"opus-bytes", model_name="small")
    assert result == {"text": "Hello world.", "language": "en", "duration": 2.5}


async def test_transcribe_before_ready_raises_and_kicks_the_load(fake_factory):
    with pytest.raises(ModelNotReadyError) as exc:
        await transcribe_audio(b"opus-bytes", model_name="small")
    assert exc.value.status == "loading"  # the failed call started the load
    await voice_stt.wait_for_load()
    result = await transcribe_audio(b"opus-bytes", model_name="small")
    assert result["text"] == "Hello world."


async def test_transcribe_with_a_different_model_raises_and_reloads(fake_factory):
    await ensure_model_loaded("small")
    await voice_stt.wait_for_load()
    with pytest.raises(ModelNotReadyError):
        await transcribe_audio(b"opus-bytes", model_name="base")
    await voice_stt.wait_for_load()
    assert voice_stt.stt_status()["model"] == "base"
    assert fake_factory.calls == ["small", "base"]


# ---------------------------------------------------- VoiceConfig coercion


def test_coerce_non_dict_yields_default():
    assert _coerce_voice(None) == default_voice_config()
    assert _coerce_voice("junk") == default_voice_config()
    assert _coerce_voice(42) == default_voice_config()


def test_coerce_empty_dict_yields_default():
    assert _coerce_voice({}) == default_voice_config()


def test_default_is_opt_in():
    config = default_voice_config()
    assert config.enabled is False
    assert config.stt_model in VOICE_STT_MODELS
    assert config.review_before_send is False


def test_coerce_valid_fields_round_trip():
    config = _coerce_voice({"enabled": True, "stt_model": "base", "review_before_send": True})
    assert config.enabled is True
    assert config.stt_model == "base"
    assert config.review_before_send is True


def test_coerce_rejects_unknown_model():
    # The whitelist is the guard: a hand-edited row can never point the
    # loader at an arbitrary HuggingFace repo id.
    config = _coerce_voice({"enabled": True, "stt_model": "evil/repo"})
    assert config.stt_model == default_voice_config().stt_model


def test_coerce_tolerates_future_fields():
    # Part 3 adds TTS fields to the same key — day-one rows must keep working.
    config = _coerce_voice({"enabled": True, "output_enabled": True, "voice": "en_US-lessac-medium"})
    assert config.enabled is True
    assert config.stt_model == default_voice_config().stt_model


def test_listen_on_summon_defaults_off_and_round_trips():
    # Part 5: hands-free listening on the global hotkey is strictly opt-in.
    assert default_voice_config().listen_on_summon is False
    # A pre-Part-5 row has no key — the default fills in.
    assert _coerce_voice({"enabled": True}).listen_on_summon is False
    assert _coerce_voice({"enabled": True, "listen_on_summon": True}).listen_on_summon is True
