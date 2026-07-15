"""
Voice TTS (Kokoro) — the engine state machine + synthesis (fake factory, no real
onnxruntime / model download, the conftest _hermetic_voice_tts backstop), the
sanitize_for_speech matrix (unchanged from earlier engines), the VoiceConfig
TTS-field coercion, and the /api/voice/speak + /api/settings/voice HTTP behavior
(httpx ASGITransport, the test_voice_api.py pattern).

Kokoro has fixed PRESET voices (no cloning): the factory takes no arguments,
ensure_engine_loaded() takes no arguments, and synthesize_speech(text, voice,
speed) passes a preset voice id + speaking speed straight through.
"""
import threading

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core import voice_tts
from app.core.app_settings import (
    VOICE_TTS_VOICES,
    _coerce_voice,
    default_voice_config,
)
from app.core.dependencies import get_db
from app.core.voice_tts import (
    TtsNotReadyError,
    ensure_engine_loaded,
    sanitize_for_speech,
    synthesize_speech,
)
from app.db.database import Base
from main import app

#: Minimal-but-real WAV header bytes — enough to assert round-tripping.
FAKE_WAV = b"RIFF....WAVEfmt fake-audio"


class FakeEngine:
    """Records every synth call. Mirrors the Kokoro engine contract:
    synthesize(text, voice, speed) and synthesize_stream(...)."""

    sr = voice_tts.DEFAULT_SR

    def __init__(self):
        self.spoken: list[str] = []
        self.synth_calls: list[dict] = []

    def synthesize(self, text, voice="af_heart", speed=1.0):
        self.spoken.append(text)
        self.synth_calls.append({"text": text, "voice": voice, "speed": speed})
        return FAKE_WAV

    def synthesize_stream(self, text, voice="af_heart", speed=1.0, should_stop=None):
        self.spoken.append(text)
        self.synth_calls.append(
            {"text": text, "voice": voice, "speed": speed, "stream": True}
        )
        yield b"\x01\x00\x02\x00"
        yield b"\x03\x00"


@pytest.fixture(autouse=True)
def fake_factory():
    """A working fake factory that records every construction + engine."""
    calls: list[object] = []
    engines: list[FakeEngine] = []

    def factory() -> FakeEngine:
        engine = FakeEngine()
        engines.append(engine)
        calls.append(engine)
        return engine

    factory.calls = calls
    factory.engines = engines
    voice_tts.TTS_ENGINE_FACTORY = factory
    yield factory


@pytest_asyncio.fixture
async def client():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_db():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
    await engine.dispose()


async def _enable_voice(client, **overrides):
    payload = {
        "enabled": True,
        "stt_model": "small",
        "review_before_send": False,
        "output_enabled": True,
        "voice": "af_heart",
        "speak_proactive": False,
        "speak_all_responses": False,
        "listen_on_summon": False,
        "tts_speed": 1.0,
    }
    payload.update(overrides)
    r = await client.put("/api/settings/voice", json=payload)
    assert r.status_code == 200
    return r.json()


# ------------------------------------------------------------ state machine


async def test_initial_status_is_not_loaded():
    status = voice_tts.tts_status()
    assert status["status"] == "not_loaded"
    assert status["voice"] is None and status["error"] is None and status["progress"] is None


async def test_ensure_kicks_a_background_load(fake_factory):
    status = await ensure_engine_loaded()
    assert status["status"] == "loading"
    await voice_tts.wait_for_tts_load()
    assert voice_tts.tts_status()["status"] == "ready"
    assert len(fake_factory.calls) == 1


async def test_ensure_is_idempotent_when_ready(fake_factory):
    await ensure_engine_loaded()
    await voice_tts.wait_for_tts_load()
    status = await ensure_engine_loaded()
    assert status["status"] == "ready"
    assert len(fake_factory.calls) == 1  # model not built twice


async def test_load_failure_is_error_and_retryable():
    attempts: list[int] = []

    def flaky() -> FakeEngine:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("weights download dropped")
        return FakeEngine()

    voice_tts.TTS_ENGINE_FACTORY = flaky
    await ensure_engine_loaded()
    await voice_tts.wait_for_tts_load()
    status = voice_tts.tts_status()
    assert status["status"] == "error"
    assert "download dropped" in status["error"]
    # The next kick must retry, never wedge.
    await ensure_engine_loaded()
    await voice_tts.wait_for_tts_load()
    assert voice_tts.tts_status()["status"] == "ready"
    assert len(attempts) == 2


async def test_synthesize_returns_wav_bytes(fake_factory):
    await ensure_engine_loaded()
    await voice_tts.wait_for_tts_load()
    wav = await synthesize_speech("Hello there.")
    assert wav == FAKE_WAV
    assert fake_factory.engines[0].spoken == ["Hello there."]


async def test_synthesize_passes_voice_and_speed(fake_factory):
    await ensure_engine_loaded()
    await voice_tts.wait_for_tts_load()
    await synthesize_speech("Hi.", voice="am_michael", speed=1.5)
    call = fake_factory.engines[0].synth_calls[0]
    assert call["voice"] == "am_michael"
    assert call["speed"] == 1.5


async def test_concurrent_synthesis_is_serialized(fake_factory):
    """The frontend keeps ~2 /speak requests in flight; _SYNTH_LOCK serializes
    them so the shared engine only ever runs one synthesis at a time."""
    import asyncio
    import time as _time

    class OverlapEngine(FakeEngine):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.max_active = 0
            self._guard = threading.Lock()

        def synthesize(self, text, voice="af_heart", speed=1.0):
            with self._guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            _time.sleep(0.05)
            with self._guard:
                self.active -= 1
            return super().synthesize(text, voice, speed)

    engines: list[OverlapEngine] = []

    def factory() -> OverlapEngine:
        engine = OverlapEngine()
        engines.append(engine)
        return engine

    voice_tts.TTS_ENGINE_FACTORY = factory
    await ensure_engine_loaded()
    await voice_tts.wait_for_tts_load()
    await asyncio.gather(
        synthesize_speech("First sentence."),
        synthesize_speech("Second sentence."),
        synthesize_speech("Third sentence."),
    )
    assert engines[0].max_active == 1  # never two engine calls at once
    assert len(engines[0].spoken) == 3  # all still ran


async def test_all_engine_work_runs_on_one_thread():
    """Load+warmup and EVERY synthesis share the one dedicated TTS thread
    (_TTS_EXECUTOR) — bounds total thread count next to whisper/fastembed and
    keeps onnxruntime's pool from spinning up on arbitrary asyncio threads."""

    class ThreadRecordingEngine(FakeEngine):
        def __init__(self):
            super().__init__()
            self.threads: list[int] = [threading.get_ident()]  # factory thread

        def synthesize(self, text, voice="af_heart", speed=1.0):
            self.threads.append(threading.get_ident())
            return super().synthesize(text, voice, speed)

    engines: list[ThreadRecordingEngine] = []

    def factory() -> ThreadRecordingEngine:
        engine = ThreadRecordingEngine()
        engines.append(engine)
        return engine

    voice_tts.TTS_ENGINE_FACTORY = factory
    await ensure_engine_loaded()
    await voice_tts.wait_for_tts_load()
    await synthesize_speech("First sentence.")
    await synthesize_speech("Second sentence.")
    threads = engines[0].threads
    assert len(threads) == 3  # factory + 2 synths
    assert len(set(threads)) == 1  # all on the one dedicated TTS thread


async def test_synthesize_before_ready_raises_and_kicks_the_load(fake_factory):
    with pytest.raises(TtsNotReadyError) as exc:
        await synthesize_speech("Hello.")
    assert exc.value.status == "loading"  # the failed call started the load
    await voice_tts.wait_for_tts_load()
    wav = await synthesize_speech("Hello.")
    assert wav == FAKE_WAV


# ------------------------------------------------------- sanitize_for_speech


def test_sanitize_plain_text_untouched():
    assert sanitize_for_speech("Hello there. How are you?") == "Hello there. How are you?"


def test_sanitize_empty():
    assert sanitize_for_speech("") == ""
    assert sanitize_for_speech("   \n\n  ") == ""


def test_sanitize_code_fence_omitted():
    text = "Here is the script:\n```python\nprint('hi')\n```\nRun it."
    assert sanitize_for_speech(text) == "Here is the script: (code omitted) Run it."


def test_sanitize_unclosed_fence_omitted():
    text = "Sure:\n```bash\nrm -rf build"
    assert sanitize_for_speech(text) == "Sure: (code omitted)"


def test_sanitize_inline_code_keeps_text():
    assert sanitize_for_speech("Run `npm install` first.") == "Run npm install first."


def test_sanitize_link_speaks_label():
    text = "See [the docs](https://example.com/docs/page) for more."
    assert sanitize_for_speech(text) == "See the docs for more."


def test_sanitize_image_speaks_alt():
    assert sanitize_for_speech("![a chart](chart.png) shows it") == "a chart shows it"


def test_sanitize_bare_url_speaks_hostname():
    text = "It's at https://api.example.com/v1/users?id=3 now."
    assert sanitize_for_speech(text) == "It's at api.example.com now."


def test_sanitize_headers_bullets_emphasis_stripped():
    text = "## Results\n- **first** item\n* _second_ item\n1. third item\n• fourth"
    assert sanitize_for_speech(text) == "Results first item second item third item fourth"


def test_sanitize_blockquote_and_table_stripped():
    text = "> quoted\n| Name | Size |\n|------|------|\n| a.txt | 2KB |"
    assert sanitize_for_speech(text) == "quoted Name Size a.txt 2KB"


def test_sanitize_windows_path_to_basename():
    text = r"Deleted C:\Users\DELL\Desktop\phase3test\notes.txt for you."
    assert sanitize_for_speech(text) == "Deleted notes.txt for you."


def test_sanitize_posix_and_home_paths_to_basename():
    assert sanitize_for_speech("Saved to ~/projects/jarvis/summary.md today") == (
        "Saved to summary.md today"
    )
    assert sanitize_for_speech("see backend/app/core/voice_tts.py there") == (
        "see voice_tts.py there"
    )


def test_sanitize_snake_case_survives():
    assert sanitize_for_speech("the search_files tool ran") == "the search_files tool ran"


def test_sanitize_emoji_dropped():
    assert sanitize_for_speech("Happy birthday 🎂🎉 to Jamil!") == "Happy birthday to Jamil!"


def test_sanitize_pure_scaffolding_yields_empty():
    assert sanitize_for_speech("```\ncode\n```") == "(code omitted)"
    assert sanitize_for_speech("🎂🎉") == ""


# ---------------------------------------------- VoiceConfig TTS-field coercion


def test_default_tts_fields():
    config = default_voice_config()
    assert config.output_enabled is True  # ON once voice itself is enabled
    assert config.voice == "af_heart"  # default preset voice
    assert config.tts_speed == 1.0


def test_coerce_part2_shaped_row_gets_tts_defaults():
    # A minimal row has no TTS keys — defaults fill in.
    config = _coerce_voice({"enabled": True, "stt_model": "base", "review_before_send": True})
    assert config.output_enabled is True
    assert config.voice == "af_heart"
    assert config.tts_speed == 1.0


def test_coerce_ignores_removed_chatterbox_fields():
    # An existing row from the Chatterbox era still carries the removed knobs and
    # a clone-id voice — unknown keys are ignored, the clone id (not a preset)
    # falls back to the default, and nothing crashes.
    config = _coerce_voice({
        "enabled": True,
        "output_enabled": False,
        "voice": "clone-abc-123",
        "tts_exaggeration": 0.9,
        "tts_cfg_weight": 0.2,
        "tts_device": "cuda",
        "tts_fast": False,
    })
    assert config.output_enabled is False
    assert config.voice == "af_heart"  # clone id is not a valid preset → default
    assert config.tts_speed == 1.0


def test_coerce_accepts_whitelisted_voice():
    config = _coerce_voice({"voice": "am_michael"})
    assert config.voice == "am_michael"


def test_coerce_rejects_unknown_voice():
    config = _coerce_voice({"voice": "zz_bogus"})
    assert config.voice == "af_heart"


def test_coerce_clamps_speed():
    assert _coerce_voice({"tts_speed": 5}).tts_speed == 2.0
    assert _coerce_voice({"tts_speed": 0.1}).tts_speed == 0.5
    assert _coerce_voice({"tts_speed": "nope"}).tts_speed == 1.0


# ----------------------------------------------------------- /api/voice/speak


async def test_speak_refused_when_voice_disabled(client):
    r = await client.post("/api/voice/speak", json={"text": "Hello."})
    assert r.status_code == 400
    assert "disabled" in r.json()["detail"]


async def test_speak_refused_when_output_disabled(client):
    await _enable_voice(client, output_enabled=False)
    r = await client.post("/api/voice/speak", json={"text": "Hello."})
    assert r.status_code == 400
    assert "output" in r.json()["detail"]


async def test_speak_returns_wav(client, fake_factory):
    await _enable_voice(client)
    await voice_tts.wait_for_tts_load()
    r = await client.post("/api/voice/speak", json={"text": "Hello there."})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.content == FAKE_WAV
    assert fake_factory.engines[0].spoken == ["Hello there."]


async def test_speak_sanitizes_by_default(client, fake_factory):
    await _enable_voice(client)
    await voice_tts.wait_for_tts_load()
    r = await client.post("/api/voice/speak", json={
        "text": "Run this:\n```bash\nls\n```\nDone **now**.",
    })
    assert r.status_code == 200
    assert fake_factory.engines[0].spoken == ["Run this: (code omitted) Done now."]


async def test_speak_threads_configured_voice_and_speed(client, fake_factory):
    await _enable_voice(client, voice="bf_emma", tts_speed=1.25)
    await voice_tts.wait_for_tts_load()
    r = await client.post("/api/voice/speak", json={"text": "Hello there."})
    assert r.status_code == 200
    call = fake_factory.engines[0].synth_calls[0]
    assert call["voice"] == "bf_emma"
    assert call["speed"] == 1.25


async def test_speak_raw_skips_the_sanitizer(client, fake_factory):
    await _enable_voice(client)
    await voice_tts.wait_for_tts_load()
    r = await client.post("/api/voice/speak", json={"text": "keep **markdown**", "raw": True})
    assert r.status_code == 200
    assert fake_factory.engines[0].spoken == ["keep **markdown**"]


async def test_speak_sanitized_to_empty_is_204(client, fake_factory):
    await _enable_voice(client)
    await voice_tts.wait_for_tts_load()
    r = await client.post("/api/voice/speak", json={"text": "🎂🎉"})
    assert r.status_code == 204
    assert fake_factory.engines[0].spoken == []  # nothing reached the engine


async def test_speak_empty_text_is_400(client):
    await _enable_voice(client)
    r = await client.post("/api/voice/speak", json={"text": "   "})
    assert r.status_code == 400


async def test_speak_oversize_text_is_400(client):
    await _enable_voice(client)
    r = await client.post("/api/voice/speak", json={"text": "x" * 2001})
    assert r.status_code == 400
    assert "too long" in r.json()["detail"]


async def test_speak_while_engine_is_loading_is_409(client):
    release = threading.Event()

    def slow_factory() -> FakeEngine:
        release.wait(timeout=5)
        return FakeEngine()

    voice_tts.TTS_ENGINE_FACTORY = slow_factory
    await _enable_voice(client)
    try:
        r = await client.post("/api/voice/speak", json={"text": "Hello."})
        assert r.status_code == 409
        assert "not ready" in r.json()["detail"]
    finally:
        release.set()
        await voice_tts.wait_for_tts_load()
    r = await client.post("/api/voice/speak", json={"text": "Hello."})
    assert r.status_code == 200


async def test_speak_synthesis_failure_is_400_not_500(client):
    class BrokenEngine:
        sr = voice_tts.DEFAULT_SR

        def synthesize(self, text, voice="af_heart", speed=1.0):
            raise ValueError("generation exploded")

    voice_tts.TTS_ENGINE_FACTORY = lambda: BrokenEngine()
    await _enable_voice(client)
    await voice_tts.wait_for_tts_load()
    r = await client.post("/api/voice/speak", json={"text": "Hello."})
    assert r.status_code == 400
    assert "Could not synthesize" in r.json()["detail"]


# ---------------------------------------------------- /api/voice/speak/stream


async def test_synthesize_speech_stream_yields_chunks(fake_factory):
    await ensure_engine_loaded()
    await voice_tts.wait_for_tts_load()
    chunks = [c async for c in voice_tts.synthesize_speech_stream("Hi there.")]
    assert chunks == [b"\x01\x00\x02\x00", b"\x03\x00"]
    assert fake_factory.engines[0].synth_calls[0]["stream"] is True


async def test_synthesize_speech_stream_before_ready_raises_and_kicks(fake_factory):
    with pytest.raises(TtsNotReadyError):
        async for _ in voice_tts.synthesize_speech_stream("Hi."):
            pass  # pragma: no cover
    await voice_tts.wait_for_tts_load()
    chunks = [c async for c in voice_tts.synthesize_speech_stream("Hi.")]
    assert chunks


async def test_speak_stream_returns_pcm_chunks(client, fake_factory):
    await _enable_voice(client)
    await voice_tts.wait_for_tts_load()
    r = await client.post("/api/voice/speak/stream", json={"text": "Hello there."})
    assert r.status_code == 200
    assert r.headers["x-sample-rate"] == str(voice_tts.DEFAULT_SR)
    assert r.headers["x-audio-format"] == "pcm_s16le"
    assert r.content == b"\x01\x00\x02\x00\x03\x00"
    assert fake_factory.engines[0].synth_calls[0]["stream"] is True


async def test_speak_stream_refused_when_output_disabled(client):
    await _enable_voice(client, output_enabled=False)
    r = await client.post("/api/voice/speak/stream", json={"text": "Hello."})
    assert r.status_code == 400
    assert "output" in r.json()["detail"]


async def test_speak_stream_sanitized_to_empty_is_204(client, fake_factory):
    await _enable_voice(client)
    await voice_tts.wait_for_tts_load()
    r = await client.post("/api/voice/speak/stream", json={"text": "🎂🎉"})
    assert r.status_code == 204
    assert fake_factory.engines[0].spoken == []


async def test_speak_stream_while_engine_is_loading_is_409(client):
    release = threading.Event()

    def slow_factory() -> FakeEngine:
        release.wait(timeout=5)
        return FakeEngine()

    voice_tts.TTS_ENGINE_FACTORY = slow_factory
    await _enable_voice(client)
    try:
        r = await client.post("/api/voice/speak/stream", json={"text": "Hello."})
        assert r.status_code == 409  # priming surfaced not-ready BEFORE any body
        assert "not ready" in r.json()["detail"]
    finally:
        release.set()
        await voice_tts.wait_for_tts_load()
    r = await client.post("/api/voice/speak/stream", json={"text": "Hello."})
    assert r.status_code == 200


async def test_speak_stream_failure_before_first_chunk_is_400(client):
    class BrokenStreamEngine:
        sr = voice_tts.DEFAULT_SR

        def synthesize_stream(self, text, voice="af_heart", speed=1.0, should_stop=None):
            raise ValueError("stream exploded")
            yield b""  # pragma: no cover — makes this a generator function

    voice_tts.TTS_ENGINE_FACTORY = lambda: BrokenStreamEngine()
    await _enable_voice(client)
    await voice_tts.wait_for_tts_load()
    r = await client.post("/api/voice/speak/stream", json={"text": "Hello."})
    assert r.status_code == 400
    assert "Could not synthesize" in r.json()["detail"]


# --------------------------------------------- settings + status (TTS half)


async def test_settings_default_includes_tts_fields(client):
    r = await client.get("/api/settings/voice")
    assert r.status_code == 200
    body = r.json()
    assert body["output_enabled"] is True
    assert body["voice"] == "af_heart"
    assert body["tts_speed"] == 1.0
    assert body["voices"] == [{"id": vid, "label": label} for vid, label in VOICE_TTS_VOICES]
    assert "clones" not in body and "tts_devices" not in body
    assert body["tts_status"]["status"] == "not_loaded"


async def test_settings_part2_shaped_put_still_works(client):
    r = await client.put("/api/settings/voice", json={
        "enabled": False, "stt_model": "base", "review_before_send": True,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["stt_model"] == "base"
    assert body["output_enabled"] is True  # defaulted, not dropped
    assert body["voice"] == "af_heart"  # defaulted, not dropped
    assert body["tts_speed"] == 1.0


async def test_settings_put_voice_and_speed_round_trip(client):
    body = await _enable_voice(client, enabled=False, voice="bm_george", tts_speed=0.8)
    assert body["voice"] == "bm_george"
    assert body["tts_speed"] == 0.8
    r = await client.get("/api/settings/voice")
    assert r.json()["voice"] == "bm_george"
    assert r.json()["tts_speed"] == 0.8


async def test_settings_put_unknown_voice_falls_back_to_default(client):
    body = await _enable_voice(client, enabled=False, voice="does-not-exist")
    assert body["voice"] == "af_heart"


async def test_settings_put_enable_kicks_the_tts_load(client, fake_factory):
    body = await _enable_voice(client)
    assert body["tts_status"]["status"] in ("loading", "ready")
    await voice_tts.wait_for_tts_load()
    assert len(fake_factory.calls) == 1


async def test_settings_put_output_disabled_does_not_kick_tts(client, fake_factory):
    await _enable_voice(client, output_enabled=False)
    assert fake_factory.calls == []
    assert voice_tts.tts_status()["status"] == "not_loaded"


async def test_status_carries_the_tts_half(client, fake_factory):
    await _enable_voice(client)
    await voice_tts.wait_for_tts_load()
    r = await client.get("/api/voice/status")
    assert r.status_code == 200
    body = r.json()
    assert body["tts"]["status"] == "ready"
    assert body["tts"]["configured_voice"] == "af_heart"
    assert body["tts"]["output_enabled"] is True
