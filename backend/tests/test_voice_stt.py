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
    def __init__(self, text: str, no_speech_prob: float | None = None):
        self.text = text
        if no_speech_prob is not None:
            self.no_speech_prob = no_speech_prob


class FakeModel:
    """⚠️ RECORDS THE DECODE OPTIONS. It used to swallow them in `**kwargs`, which
    made it unable to express the one thing that matters most about this call:
    `language=`. Whisper's language ID turned spoken English into Arabic script
    live (2026-08-12) precisely because nothing was passed, and a fake that
    accepts anything would have gone on passing whatever the fix did or did not
    do. `last_kwargs` is what the tests assert against."""

    def __init__(self, name: str):
        self.name = name
        self.last_kwargs: dict = {}
        self.segments: list[FakeSegment] = [FakeSegment(" Hello "), FakeSegment("world. ")]

    def transcribe(self, audio, **kwargs):
        self.last_kwargs = dict(kwargs)
        return iter(self.segments), FakeInfo()


@pytest.fixture(autouse=True)
def fake_factory():
    """A working fake factory that records every construction."""
    calls: list[str] = []

    models: list[FakeModel] = []

    def factory(name: str) -> FakeModel:
        calls.append(name)
        model = FakeModel(name)
        models.append(model)
        return model

    factory.calls = calls
    factory.models = models
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
    assert result == {
        "text": "Hello world.",
        "language": "en",
        "duration": 2.5,
        # Whisper's own "was that speech?" verdict; 0.0 when the segments carry
        # none, which is what a model that never doubted itself reports.
        "no_speech_prob": 0.0,
    }


# ------------------------------------------- the spoken language is PINNED
# Live defect (2026-08-12): "there were some arabic or urdu words when i spoke
# in the voice mode, my words were converted to arabic". The transcribe call
# passed no `language`, so Whisper ran language ID on every short utterance.


async def test_the_configured_language_is_passed_to_whisper(fake_factory):
    await ensure_model_loaded("small")
    await voice_stt.wait_for_load()
    await transcribe_audio(b"opus-bytes", model_name="small", language="en")
    assert fake_factory.models[-1].last_kwargs["language"] == "en"


async def test_auto_restores_detection(fake_factory):
    """"auto" is still available for genuinely multilingual use — it just is not
    the default any more."""
    await ensure_model_loaded("small")
    await voice_stt.wait_for_load()
    await transcribe_audio(b"opus-bytes", model_name="small", language="auto")
    assert fake_factory.models[-1].last_kwargs["language"] is None


async def test_an_english_only_model_is_pinned_to_english_whatever_the_setting(
    fake_factory,
):
    """A `.en` model cannot transcribe Urdu, so naming it would either raise or
    produce nonsense. Choosing `small.en` IS choosing English."""
    await ensure_model_loaded("small.en")
    await voice_stt.wait_for_load()
    await transcribe_audio(b"opus-bytes", model_name="small.en", language="ur")
    assert fake_factory.models[-1].last_kwargs["language"] == "en"


async def test_previous_text_is_never_fed_forward(fake_factory):
    """condition_on_previous_text is what makes Whisper fall into repeat loops
    and invent filler ("Thank you.") on near-silence. A voice turn is a
    standalone utterance, so the feature has nothing to offer and a real failure
    mode to cause — and an open mic would send that filler as a chat message."""
    await ensure_model_loaded("small")
    await voice_stt.wait_for_load()
    await transcribe_audio(b"opus-bytes", model_name="small")
    assert fake_factory.models[-1].last_kwargs["condition_on_previous_text"] is False


async def test_no_speech_prob_is_averaged_over_the_segments(fake_factory):
    await ensure_model_loaded("small")
    await voice_stt.wait_for_load()
    model = fake_factory.models[-1]
    model.segments = [
        FakeSegment(" Thank you. ", no_speech_prob=0.9),
        FakeSegment("you. ", no_speech_prob=0.7),
    ]
    result = await transcribe_audio(b"opus-bytes", model_name="small")
    assert result["no_speech_prob"] == pytest.approx(0.8)


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


# ------------------------------------------------- CPU engine thread bounding
#
# voice_stt and voice_tts each asked onnxruntime/ctranslate2 for
# os.cpu_count() threads — 16 on the dev laptop. Both reach the CPU path by
# SILENT FALLBACK from a failed CUDA init, so a VRAM squeeze on a 6 GB laptop
# GPU turned itself into a whole-machine CPU saturation event.

def test_cpu_worker_threads_leaves_the_machine_usable():
    """The engine must never claim every logical core."""
    import os
    from app.core.gpu_bootstrap import cpu_worker_threads

    logical = os.cpu_count() or 4
    threads = cpu_worker_threads()
    assert 1 <= threads < logical, (
        f"cpu_worker_threads()={threads} on a {logical}-core box - an inference "
        "engine taking every core is what freezes the laptop"
    )


def test_cpu_worker_threads_never_returns_zero_on_a_small_box(monkeypatch):
    """Headroom subtraction must not starve a 1- or 2-core machine: zero or a
    negative thread count is a crash or a hang, not a slow engine."""
    from app.core import gpu_bootstrap

    for logical in (1, 2, 3, 4, 8, 16, 32):
        monkeypatch.setattr(gpu_bootstrap.os, "cpu_count", lambda n=logical: n)
        assert gpu_bootstrap.cpu_worker_threads() >= 1, f"failed at {logical} cores"


def test_whisper_cpu_path_uses_the_bounded_count(monkeypatch):
    """The CPU branch must pass the bounded count through to WhisperModel."""
    from app.core import voice_stt

    seen = {}

    class _FakeModel:
        def __init__(self, name, **kwargs):
            seen.update(kwargs)

    import faster_whisper
    monkeypatch.setattr(faster_whisper, "WhisperModel", _FakeModel)
    voice_stt._build_model("small", "cpu", "int8")

    import os
    assert seen["cpu_threads"] == voice_stt.cpu_worker_threads()
    assert seen["cpu_threads"] < (os.cpu_count() or 4)
