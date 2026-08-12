"""
Jarvis OS — Local text-to-speech (Kokoro, via kokoro-onnx)

Kokoro (an 82M-param TTS, Apache-2.0) behind the same injectable factory the
Piper/Chatterbox engines used (the voice_stt / google_services pattern):
TTS_ENGINE_FACTORY is the ONE seam tests swap — the suite never imports
onnxruntime, downloads weights, or synthesizes real audio. Everything is local;
the text of a response never leaves the machine to be spoken.

WHY kokoro-onnx (not the official `kokoro` package): it runs inference through
**onnxruntime and needs NO PyTorch**. The app already loads onnxruntime today
(fastembed embeddings) beside ctranslate2 (faster-whisper), so this adds no new
runtime class — and dropping torch removed the libiomp5md.dll OpenMP conflict
that used to crash the backend at startup (2026-07-15), rather than trading it
for a new one.

Kokoro has FIXED PRESET VOICES only (no voice cloning — that feature was
Chatterbox-specific and was removed). A "voice" is just a whitelisted preset id
(VoiceConfig.voice, validated in app_settings against VOICE_TTS_VOICES); it is
passed straight through to the engine, no per-voice model reload. Speaking speed
is VoiceConfig.tts_speed.

Engine lifecycle is the Part 1 state machine verbatim (not_loaded / loading /
ready / error) because the FIRST load doubles as the model download (~models
land under ~/.jarvis/kokoro) and must never block a request: ensure_engine_loaded()
kicks a referenced background task and callers poll /api/voice/status. An `error`
state is retryable — a failed load heals on the next kick, never wedges speech
until a restart. The factory runs ONE warmup synth before returning, so `ready`
means genuinely ready.

Model + voice files live under ~/.jarvis/kokoro (the ~/.jarvis home convention:
trash, google token, whisper weights). Their filenames are module constants —
never user-supplied — so the on-demand downloader can never be pointed at an
arbitrary URL (the download-security convention).
"""
import asyncio
import functools
import io
import os
import re
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from loguru import logger

from app.core.gpu_bootstrap import (
    cpu_worker_threads,
    cuda_available,
    register_cuda_dll_dirs,
)

#: Where Kokoro's ONNX model + voice-embeddings pack live.
KOKORO_DIR = Path.home() / ".jarvis" / "kokoro"

#: Kokoro synthesizes at 24 kHz — same as the previous engine, so the
#: /speak/stream X-Sample-Rate header and the browser playback are unchanged.
DEFAULT_SR = 24000

#: A safe fallback voice id (matches VOICE_TTS_VOICES' default in app_settings).
DEFAULT_VOICE = "af_heart"

#: The model + voice-pack files, and the pinned release they download from.
#: These are CONSTANTS (never user-supplied): a hand-edited settings row can
#: never point the downloader at an arbitrary URL.
_MODEL_FILENAME = "kokoro-v1.0.onnx"
_VOICES_FILENAME = "voices-v1.0.bin"
_RELEASE_BASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
)

#: ~50 ms PCM frames when streaming — small enough for smooth Web Audio
#: scheduling, large enough to keep per-chunk overhead negligible.
_STREAM_FRAME_MS = 50


class _KokoroEngine:
    """Wraps a loaded kokoro-onnx `Kokoro` into the factory contract. A voice is
    a preset id passed straight to `create()`; there is no per-voice reload."""

    def __init__(self, model: Any, sr: int):
        self._model = model
        self.sr = sr

    def synthesize(self, text: str, voice: str = DEFAULT_VOICE, speed: float = 1.0) -> bytes:
        samples, sr = self._model.create(
            text, voice=voice or DEFAULT_VOICE, speed=speed, lang="en-us"
        )
        return _pcm16_wav(samples, int(sr or self.sr))

    def synthesize_stream(
        self,
        text: str,
        voice: str = DEFAULT_VOICE,
        speed: float = 1.0,
        should_stop: Optional[Callable[[], bool]] = None,
    ):
        """Generator of raw PCM16 (s16le mono) byte frames. Kokoro is fast
        enough that we synthesize the (already sentence-sized) text once and
        emit it in ~50 ms frames — smooth for Web Audio, and the elaborate
        intra-sentence streaming the Chatterbox engine needed is gone. A
        `should_stop` flip (client abort / barge-in) ends the stream between
        frames."""
        samples, sr = self._model.create(
            text, voice=voice or DEFAULT_VOICE, speed=speed, lang="en-us"
        )
        pcm = _to_pcm16(samples)
        frame_bytes = max(2, int(int(sr or self.sr) * _STREAM_FRAME_MS / 1000) * 2)
        for start in range(0, len(pcm), frame_bytes):
            if should_stop is not None and should_stop():
                return
            yield pcm[start:start + frame_bytes]


# --------------------------------------------------------------- audio helpers

def _to_pcm16(samples: Any) -> bytes:
    """float32 audio in [-1, 1] → little-endian s16le PCM bytes."""
    import numpy as np

    audio = np.asarray(samples, dtype=np.float32)
    return (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def _pcm16_wav(samples: Any, sr: int) -> bytes:
    """float32 audio → mono 16-bit PCM WAV (the /speak output contract — the
    browser <audio> element and the playback queue are untouched)."""
    pcm16 = _to_pcm16(samples)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sr))
        wf.writeframes(pcm16)
    return buf.getvalue()


# ------------------------------------------------------------- model download

def _download_file(url: str, dest: Path) -> None:
    """Stream a file to disk atomically (temp + os.replace) — a partial download
    never leaves a corrupt model in place. Used only for the whitelisted asset
    filenames above."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=None) as resp:
            resp.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    f.write(chunk)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _ensure_model_files() -> tuple[Path, Path]:
    """Return (model_path, voices_path) under ~/.jarvis/kokoro, downloading any
    that are missing/empty from the pinned release. The user may also drop the
    files in by hand — an existing non-empty file is never re-fetched."""
    model_path = KOKORO_DIR / _MODEL_FILENAME
    voices_path = KOKORO_DIR / _VOICES_FILENAME
    for path, name in ((model_path, _MODEL_FILENAME), (voices_path, _VOICES_FILENAME)):
        if path.exists() and path.stat().st_size > 0:
            continue
        url = f"{_RELEASE_BASE}/{name}"
        logger.info(f"Downloading Kokoro asset '{name}' (first load only)…")
        _download_file(url, path)
    return model_path, voices_path


def _prepare_espeak() -> None:
    """Make the bundled espeak-ng library discoverable to phonemizer (kokoro-onnx
    phonemizes via espeak). espeakng-loader ships the shared library, so no
    system espeak install is needed on Windows. Best-effort — kokoro-onnx also
    self-configures; this just removes a common first-run failure mode."""
    try:
        import espeakng_loader

        espeakng_loader.make_library_available()
    except Exception as e:  # pragma: no cover - environment dependent
        logger.debug(f"espeakng-loader setup skipped ({e}); kokoro-onnx will self-configure.")


def _build_kokoro(model_path: Path, voices_path: Path, device: str) -> tuple[Any, str]:
    """Build a Kokoro engine on the requested device via an explicit
    onnxruntime session (Kokoro.from_session). Returns (model, actual_device):
    onnxruntime silently falls back to CPU when the CUDA provider can't
    initialize, so the session's active provider is the source of truth for
    which device actually loaded. On CPU, intra-op threads are set to the core
    count (onnxruntime under-threads by default — a large CPU-path win)."""
    import onnxruntime as rt
    from kokoro_onnx import Kokoro

    want_cuda = device in ("auto", "cuda") and cuda_available()
    providers: list = []
    if want_cuda:
        providers.append(("CUDAExecutionProvider", {"device_id": 0}))
    providers.append("CPUExecutionProvider")

    so = rt.SessionOptions()
    if not want_cuda:
        # Bounded, not "all cores" — see gpu_bootstrap.cpu_worker_threads. This
        # path also runs after a silent CUDA fallback, so it is exactly when the
        # machine can least afford to have every logical core taken.
        so.intra_op_num_threads = cpu_worker_threads()

    session = rt.InferenceSession(str(model_path), sess_options=so, providers=providers)
    model = Kokoro.from_session(session, str(voices_path))
    active = session.get_providers()
    actual = "cuda" if active and active[0] == "CUDAExecutionProvider" else "cpu"
    return model, actual


def _warm(engine: "_KokoroEngine") -> bool:
    """Run one throwaway synth so the first REAL sentence is fast (onnxruntime
    session/graph + CUDA context init are paid here, inside the background
    load). Returns success; a CUDA warmup failure signals a CPU rebuild."""
    try:
        import time

        started = time.perf_counter()
        engine.synthesize("Voice is ready.", DEFAULT_VOICE, 1.0)
        logger.info(f"TTS warmup synth completed in {time.perf_counter() - started:.1f}s")
        return True
    except Exception as e:
        logger.warning(f"TTS warmup synth failed: {e}")
        return False


def _default_engine_factory() -> Any:
    """Construct a real Kokoro engine (download-on-demand + load) on the
    configured device (read from the module `_device_pref`, set by
    ensure_engine_loaded before the load). A forced/auto CUDA load or warmup
    that fails rebuilds on CPU in code — voice never wedges on a bad device.
    Imported lazily so merely importing this module never pays for the
    onnxruntime import chain, let alone a weights download."""
    global _loaded_device
    register_cuda_dll_dirs()
    _prepare_espeak()
    model_path, voices_path = _ensure_model_files()
    device = _device_pref

    model, actual = _build_kokoro(model_path, voices_path, device)
    engine = _KokoroEngine(model, DEFAULT_SR)
    if not _warm(engine) and actual == "cuda":
        logger.warning("Kokoro CUDA warmup failed; rebuilding on CPU.")
        model, actual = _build_kokoro(model_path, voices_path, "cpu")
        engine = _KokoroEngine(model, DEFAULT_SR)
        _warm(engine)  # best-effort on the CPU fallback
    _loaded_device = actual
    logger.info(f"✅ Kokoro TTS loaded on {actual}")
    return engine


#: The injectable seam — tests swap this for a fake; nothing else in the
#: codebase constructs a TTS engine any other way. The returned object must
#: expose sync `synthesize(text, voice, speed) -> bytes` (WAV) and
#: `synthesize_stream(text, voice, speed, should_stop)` (generator of raw PCM16
#: byte frames); `sr` is read when present (DEFAULT_SR otherwise).
TTS_ENGINE_FACTORY: Callable[[], Any] = _default_engine_factory


class TtsNotReadyError(Exception):
    """The speech engine is not loaded yet (or failed to load). Carries the
    current status so the API layer can report something actionable."""

    def __init__(self, status: str, detail: str = ""):
        self.status = status
        self.detail = detail
        super().__init__(detail or f"TTS engine not ready (status: {status})")


# ------------------------------------------------------------- module state
# All mutation happens on the backend's single asyncio loop (the load itself
# runs in a worker thread, but status flips happen before/after the await),
# so plain module globals are race-free here — the voice_stt discipline.
_engine: Any = None
_status: str = "not_loaded"  # not_loaded | loading | ready | error
_error: Optional[str] = None
_load_task: Optional[asyncio.Task] = None
#: Device selection: `_device_pref` (auto/cpu/cuda) is what the factory reads at
#: load time; `_loaded_pref` is the pref that's actually loaded (a change
#: reloads); `_loaded_device` is the CONCRETE device the session ended up on
#: ("cpu"/"cuda"/None before any load), reported by tts_status().
_device_pref: str = "auto"
_desired_pref: Optional[str] = None
_loaded_pref: Optional[str] = None
_loaded_device: Optional[str] = None
#: Serializes ALL engine use: synthesis is CPU-bound onnxruntime work, and one
#: serial queue keeps it predictable next to the STT/embedding models sharing
#: the machine (the frontend keeps ~2 /speak requests in flight — they queue
#: here rather than oversubscribing the CPU).
_SYNTH_LOCK: asyncio.Lock = asyncio.Lock()
#: A single dedicated worker thread for all engine work (load + synthesis) —
#: keeps onnxruntime's thread pool from being spun up on arbitrary asyncio
#: pool threads and bounds total thread count beside whisper/fastembed.
_TTS_EXECUTOR: ThreadPoolExecutor = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="jarvis-tts"
)


async def _run_on_tts_thread(fn: Callable, *args: Any) -> Any:
    """Run engine work on the one dedicated TTS thread (see _TTS_EXECUTOR)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_TTS_EXECUTOR, functools.partial(fn, *args))


def tts_status() -> dict:
    """The current engine state for /api/voice/status — purely local, no I/O.
    `voice`/`progress` are kept for shape stability with the frontend (the
    configured preset voice is reported separately by /status as
    `configured_voice`; the model download is opaque, so `progress` is always
    None → the settings card shows an indeterminate bar, like whisper STT)."""
    return {
        "status": _status,
        "voice": None,
        "error": _error,
        "progress": None,
        "device": _loaded_device,
    }


def reset_tts() -> None:
    """Test hook: back to a pristine module (default factory included)."""
    global _engine, _status, _error, _load_task
    global TTS_ENGINE_FACTORY, _SYNTH_LOCK, _TTS_EXECUTOR
    global _device_pref, _desired_pref, _loaded_pref, _loaded_device
    _engine = None
    _status = "not_loaded"
    _error = None
    _load_task = None
    _device_pref = "auto"
    _desired_pref = None
    _loaded_pref = None
    _loaded_device = None
    _SYNTH_LOCK = asyncio.Lock()
    _TTS_EXECUTOR.shutdown(wait=False)
    _TTS_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-tts")
    TTS_ENGINE_FACTORY = _default_engine_factory


async def _load(device: str) -> None:
    global _engine, _status, _error, _loaded_pref, _device_pref
    # Publish the pref the default factory reads, just before the load.
    _device_pref = device
    try:
        loaded = await _run_on_tts_thread(TTS_ENGINE_FACTORY)
    except Exception as e:
        if _desired_pref == device:
            # Retryable: the next ensure_engine_loaded() kicks a fresh attempt.
            _status = "error"
            _error = str(e)
        logger.warning(f"Voice TTS engine failed to load: {e}")
        return
    if _desired_pref != device:
        # The requested device changed while this load was in flight — the
        # newer request owns the state; discard this result.
        return
    _engine = loaded
    _loaded_pref = device
    _status = "ready"
    _error = None
    logger.info(f"✅ Voice TTS engine (Kokoro) ready on {_loaded_device}")


async def ensure_engine_loaded(device: str = "auto") -> dict:
    """Kick a background load of the model on the given device unless it is
    already ready (on that device) or currently loading. Returns immediately
    with the current status — callers poll /api/voice/status; nothing ever
    blocks on the weights download."""
    global _engine, _status, _error, _load_task, _desired_pref
    if _status == "ready" and _engine is not None and _loaded_pref == device:
        return tts_status()
    if _status == "loading":
        # A load is in flight; a device change lands on the next ensure after
        # it settles (eventual consistency beats cancelling a load).
        return tts_status()
    _engine = None
    _desired_pref = device
    _status = "loading"
    _error = None
    _load_task = asyncio.create_task(_load(device))
    return tts_status()


async def wait_for_tts_load() -> None:
    """Await the in-flight load, if any (tests + graceful verification)."""
    if _load_task is not None:
        await _load_task


def is_ready() -> bool:
    return _status == "ready" and _engine is not None


async def synthesize_speech(
    text: str, voice: str = DEFAULT_VOICE, speed: float = 1.0, device: str = "auto"
) -> bytes:
    """Synthesize one utterance → WAV bytes, in the given preset voice.
    Serialized under _SYNTH_LOCK — see the lock's comment.

    Not ready → kick the load on the configured device (self-healing: a stray
    early call starts the download instead of failing inertly) and raise
    TtsNotReadyError for the API layer to surface as "still loading, try again
    shortly"."""
    if _status != "ready" or _engine is None:
        await ensure_engine_loaded(device)
        raise TtsNotReadyError(_status, _error or "")
    async with _SYNTH_LOCK:
        return await _run_on_tts_thread(_engine.synthesize, text, voice, speed)


def engine_sample_rate() -> int:
    """The loaded engine's output sample rate (DEFAULT_SR when unknown) — the
    /speak/stream response header; raw PCM carries no header of its own."""
    return int(getattr(_engine, "sr", DEFAULT_SR) or DEFAULT_SR)


async def synthesize_speech_stream(
    text: str, voice: str = DEFAULT_VOICE, speed: float = 1.0, device: str = "auto"
):
    """Async generator of raw PCM16 (s16le mono at engine_sample_rate()) byte
    frames — the playback side schedules them with Web Audio.

    Holds _SYNTH_LOCK for the WHOLE stream; the producer runs on the dedicated
    TTS thread and frames cross to the loop via an asyncio.Queue. A consumer
    that goes away (client disconnect / barge-in abort) flips a stop event the
    frame loop checks, so an abandoned stream stops promptly. Not-ready handling
    is the synthesize_speech contract."""
    if _status != "ready" or _engine is None:
        await ensure_engine_loaded(device)
        raise TtsNotReadyError(_status, _error or "")

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()
    done = object()  # sentinel

    def _producer() -> None:
        try:
            for chunk in _engine.synthesize_stream(text, voice, speed, stop.is_set):
                if stop.is_set():
                    break
                loop.call_soon_threadsafe(queue.put_nowait, chunk)
            loop.call_soon_threadsafe(queue.put_nowait, done)
        except BaseException as e:  # forwarded to the consumer below
            loop.call_soon_threadsafe(queue.put_nowait, e)

    async with _SYNTH_LOCK:
        producer_future = loop.run_in_executor(_TTS_EXECUTOR, _producer)
        try:
            while True:
                item = await queue.get()
                if item is done:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            # Consumer gone or stream finished: stop the producer and wait for
            # the TTS thread to actually settle BEFORE releasing the lock.
            stop.set()
            await producer_future


# ==================================================== markdown → speech text
# The engine never reads raw markdown aloud: the /speak endpoint runs every
# text through this deterministic cleaner (opt-out via the `raw` flag). Pure
# and heavily tested — no LLM call, same input → same speech, always.

_FENCED_CODE_RE = re.compile(r"```.*?```", re.S)
_UNCLOSED_FENCE_RE = re.compile(r"```.*\Z", re.S)
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]*)`")
_BARE_URL_RE = re.compile(r"https?://([^/\s)>\]\"']+)[^\s)>\]\"']*")
_TABLE_RULE_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", re.M)
_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.M)
_BULLET_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.M)
_EMPHASIS_RE = re.compile(r"\*\*|__|~~|\*")
_LONE_UNDERSCORE_RE = re.compile(r"(?<!\w)_+|_+(?!\w)")
#: Path-like tokens: drive-rooted (C:\…), UNC (\\server\…), home (~/…), or
#: any token with ≥2 separators. Reduced to their basename — "the file
#: C:\Users\DELL\Desktop\notes.txt" is spoken "the file notes.txt".
_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|\\\\|~[\\/])[^\s\"'`|]+"
    r"|(?<![\w.:/\\])(?:[\w.\-]+[\\/]){2,}[\w.\-]+"
)
#: Emoji / pictographs / dingbats / variation selectors — dropped outright.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # emoticons, pictographs, transport, supplemental
    "\U00002600-\U000027BF"  # misc symbols + dingbats
    "\U0001F1E6-\U0001F1FF"  # regional indicators (flags)
    "\U00002B00-\U00002BFF"  # misc symbols and arrows
    "\U0000FE0E\U0000FE0F"   # variation selectors
    "\U0000200D"             # zero-width joiner
    "]+"
)
#: `file(s)` / `item(s)` / `step(s)` — the plural-agnostic form this codebase's
#: deterministic renderers use everywhere ("Found 7 matching file(s)", "Done — 2
#: step(s) completed"). It is exactly right in writing and unreadable aloud: the
#: engine says "file open bracket s close bracket" or "file s". Spoken text has
#: no reason to hedge about number, so the plural wins.
_PAREN_PLURAL_RE = re.compile(r"\b(\w+?)\(s\)")
#: A trailing separator on a word — `sub/` (a folder, as list_directory marks
#: them), `Desktop\`. The slash is a written convention, not something to read.
_TRAILING_SEP_RE = re.compile(r"(\w)[\\/](?=\s|$)")
#: An em/en dash between two things is a spoken PAUSE, not a word. Kokoro reads
#: a bare dash inconsistently; a comma is unambiguous.
_DASH_RE = re.compile(r"\s+[—–]\s+")
_WHITESPACE_RE = re.compile(r"\s+")


def _path_to_basename(match: "re.Match[str]") -> str:
    token = match.group(0).rstrip("\\/").rstrip(".,;:")
    base = re.split(r"[\\/]+", token)[-1]
    return base or token


def sanitize_for_speech(text: str) -> str:
    """Deterministic markdown → speakable text. Code blocks are announced,
    never read; links speak their label; paths speak their basename; layout
    markers and emoji vanish. Plain prose passes through untouched (modulo
    whitespace collapsing)."""
    if not text:
        return ""
    out = text.replace("\r\n", "\n")
    # Code first: fence contents must never be visible to the later rules.
    out = _FENCED_CODE_RE.sub(" (code omitted) ", out)
    out = _UNCLOSED_FENCE_RE.sub(" (code omitted) ", out)
    out = _IMAGE_RE.sub(r"\1", out)          # image → its alt text
    out = _LINK_RE.sub(r"\1", out)           # link → its label
    out = _INLINE_CODE_RE.sub(r"\1", out)    # inline code → its text
    out = _BARE_URL_RE.sub(r"\1", out)       # bare URL → its hostname
    out = _TABLE_RULE_RE.sub(" ", out)       # |---|---| separator rows
    out = out.replace("|", " ")              # table cell pipes
    out = _HEADER_RE.sub("", out)
    out = _BLOCKQUOTE_RE.sub("", out)
    out = _BULLET_RE.sub("", out)
    out = _EMPHASIS_RE.sub("", out)
    out = _LONE_UNDERSCORE_RE.sub("", out)   # _emphasis_ but not snake_case
    out = _PATH_RE.sub(_path_to_basename, out)
    out = _EMOJI_RE.sub("", out)
    # Machine-shaped residue that survives the markdown rules above but is not
    # speech: written-plural hedges, a folder's trailing slash, a layout dash.
    out = _PAREN_PLURAL_RE.sub(r"\1s", out)
    out = _TRAILING_SEP_RE.sub(r"\1", out)
    out = _DASH_RE.sub(", ", out)
    return _WHITESPACE_RE.sub(" ", out).strip()
