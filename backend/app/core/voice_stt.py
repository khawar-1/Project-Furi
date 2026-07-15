"""
Jarvis OS — Local speech-to-text (Phase 7, Part 1; GPU-accelerated voice round)

faster-whisper behind an injectable factory (the google_services pattern):
STT_MODEL_FACTORY is the ONE seam tests swap — the suite never downloads or
loads a real model. Everything is local; audio bytes never leave the machine.

Model lifecycle is a small state machine (not_loaded / loading / ready / error)
because the FIRST load doubles as the model download (~500MB for "small") and
must never block a request: ensure_model_loaded() kicks a referenced background
task (the _prewarm_embedder pattern — the module itself keeps the task ref) and
callers poll /api/voice/status. An `error` state is retryable — the recorded
flaky-network gotcha means a failed download must heal on the next kick, never
wedge voice until a restart.

Device: transcription runs on the GPU (ctranslate2's CUDA backend) when a CUDA
device is present and the configured device is "auto"/"cuda", falling back to
CPU in code if a forced CUDA load fails — voice must never wedge on a bad
device. On a mid-range GPU this takes STT from seconds to well under half a
second. The chosen device + compute type ride on module state that the default
factory reads (the factory seam stays a bare `(model_name)` callable so the test
fakes are unchanged); the ACTUAL device that loaded is reported by stt_status().

Model weights live under ~/.jarvis/whisper (the ~/.jarvis home convention:
trash, google token, and the Kokoro TTS model files).
"""
import asyncio
import io
import os
import wave
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

from app.core.gpu_bootstrap import cuda_available, register_cuda_dll_dirs

#: Where faster-whisper stores downloaded model weights.
WHISPER_DIR = Path.home() / ".jarvis" / "whisper"

#: Greedy decode (beam_size=1) is markedly faster than the old beam_size=5 with
#: negligible accuracy loss for short command-style utterances — the dominant
#: STT latency lever after device. A module constant so it stays tunable.
STT_BEAM_SIZE = 1


def _resolve_device(pref: str) -> str:
    """A device preference ("auto"/"cpu"/"cuda") → a concrete device. "auto"
    picks CUDA when a GPU is present, else CPU."""
    if pref == "cpu":
        return "cpu"
    if pref == "cuda":
        return "cuda"
    return "cuda" if cuda_available() else "cpu"  # auto


def _resolve_compute_type(device: str, pref: str) -> str:
    """A compute-type preference → a concrete ctranslate2 compute type. "auto"
    derives it from the device: int8_float16 on CUDA (near-float16 speed at half
    the VRAM — the safe default on a laptop GPU that also drives the display),
    int8 on CPU."""
    if pref and pref != "auto":
        return pref
    return "int8_float16" if device == "cuda" else "int8"


def _build_model(model_name: str, device: str, compute_type: str) -> Any:
    from faster_whisper import WhisperModel

    kwargs: dict[str, Any] = {
        "device": device,
        "compute_type": compute_type,
        "download_root": str(WHISPER_DIR),
    }
    if device == "cpu":
        # Use all physical cores for CPU decode (the default under-threads).
        kwargs["cpu_threads"] = os.cpu_count() or 4
    return WhisperModel(model_name, **kwargs)


def _warmup(model: Any) -> None:
    """Run one throwaway transcription so the first REAL utterance is fast: the
    encoder/decoder graph + CUDA context init are paid here, inside the
    background load, before status flips ready (the TTS-warmup discipline).
    Best-effort — a warmup failure never blocks the load."""
    try:
        import time

        import numpy as np

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            # ~1s of near-silence — enough signal to force a real decode with
            # VAD off (pure silence would be skipped and skip the warmup).
            samples = (np.random.randn(16000) * 8).astype("<i2")
            wf.writeframes(samples.tobytes())
        started = time.perf_counter()
        segments, _ = model.transcribe(
            io.BytesIO(buf.getvalue()), beam_size=1, vad_filter=False
        )
        for _ in segments:  # drain the lazy generator = run the decode
            pass
        logger.info(f"STT warmup completed in {time.perf_counter() - started:.1f}s")
    except Exception as e:
        logger.warning(f"STT warmup failed (non-critical): {e}")


def _default_model_factory(model_name: str) -> Any:
    """Construct a real faster-whisper model on the configured device. Reads the
    module's `_device_pref` / `_compute_pref` (set by ensure_model_loaded before
    the load is kicked) so the injectable seam stays a bare `(model_name)`
    callable. A forced/auto CUDA load that fails falls back to CPU in code, and
    the ACTUAL device is recorded in `_loaded_device` for stt_status().

    Imported lazily so merely importing this module (main.py does at startup)
    never pays for the faster_whisper import chain, let alone a model load."""
    global _loaded_device
    register_cuda_dll_dirs()
    device = _resolve_device(_device_pref)
    compute_type = _resolve_compute_type(device, _compute_pref)
    try:
        model = _build_model(model_name, device, compute_type)
        _loaded_device = device
    except Exception as e:
        if device == "cuda":
            logger.warning(
                f"STT CUDA load failed ({e}); falling back to CPU. "
                "Set stt_device='cpu' in Settings to silence this."
            )
            model = _build_model(model_name, "cpu", "int8")
            _loaded_device = "cpu"
        else:
            raise
    logger.info(
        f"Loading Whisper '{model_name}' on {_loaded_device} "
        f"(compute={compute_type if _loaded_device == device else 'int8'})…"
    )
    _warmup(model)
    return model


#: The injectable seam — tests swap this for a fake; nothing else in the
#: codebase constructs an STT model any other way.
STT_MODEL_FACTORY: Callable[[str], Any] = _default_model_factory


class ModelNotReadyError(Exception):
    """The transcription model is not loaded yet (or failed to load). Carries
    the current status so the API layer can report something actionable."""

    def __init__(self, status: str, detail: str = ""):
        self.status = status
        self.detail = detail
        super().__init__(detail or f"STT model not ready (status: {status})")


# ------------------------------------------------------------- module state
# All mutation happens on the backend's single asyncio loop (the load itself
# runs in a worker thread, but status flips happen before/after the await),
# so plain module globals are race-free here.
_model: Any = None
_status: str = "not_loaded"  # not_loaded | loading | ready | error
_error: Optional[str] = None
_load_task: Optional[asyncio.Task] = None
#: The (model_name, device_pref, compute_pref) currently desired / actually
#: loaded — a change in ANY of the three reloads.
_desired: Optional[tuple] = None
_loaded: Optional[tuple] = None
#: The device/compute prefs the default factory reads at load time, and the
#: ACTUAL device that loaded ("cpu"/"cuda"/None before any load).
_device_pref: str = "auto"
_compute_pref: str = "auto"
_loaded_device: Optional[str] = None


def stt_status() -> dict:
    """The current model state for /api/voice/status — purely local, no I/O."""
    return {
        "status": _status,
        # The CURRENT target model (what's loading or loaded), not a stale
        # previously-loaded one — matches the pre-GPU-round semantics.
        "model": _desired[0] if _desired else (_loaded[0] if _loaded else None),
        "error": _error,
        "device": _loaded_device,
    }


def reset_stt() -> None:
    """Test hook: back to a pristine module (default factory included)."""
    global _model, _status, _error, _load_task, STT_MODEL_FACTORY
    global _desired, _loaded, _device_pref, _compute_pref, _loaded_device
    _model = None
    _status = "not_loaded"
    _error = None
    _load_task = None
    _desired = None
    _loaded = None
    _device_pref = "auto"
    _compute_pref = "auto"
    _loaded_device = None
    STT_MODEL_FACTORY = _default_model_factory


async def _load(desired: tuple) -> None:
    global _model, _status, _error, _loaded, _device_pref, _compute_pref
    model_name, device_pref, compute_pref = desired
    # Publish the prefs the default factory reads, just before the load.
    _device_pref, _compute_pref = device_pref, compute_pref
    try:
        loaded = await asyncio.to_thread(STT_MODEL_FACTORY, model_name)
    except Exception as e:
        if _desired == desired:
            # Retryable: the next ensure_model_loaded() kicks a fresh attempt.
            _status = "error"
            _error = str(e)
        logger.warning(f"Voice STT model '{model_name}' failed to load: {e}")
        return
    if _desired != desired:
        # The requested model/device changed while this load was in flight —
        # the newer request owns the state; discard this result.
        return
    _model = loaded
    _loaded = desired
    _status = "ready"
    _error = None
    logger.info(f"✅ Voice STT model '{model_name}' ready on {_loaded_device}")


async def ensure_model_loaded(
    model_name: str, device: str = "auto", compute_type: str = "auto"
) -> dict:
    """Kick a background load of `model_name` on the given device unless it is
    already ready or loading. Returns immediately with the current status —
    callers poll /api/voice/status; nothing ever blocks on the download."""
    global _model, _status, _error, _load_task, _desired
    desired = (model_name, device, compute_type)
    if _status == "ready" and _loaded == desired and _model is not None:
        return stt_status()
    if _status == "loading":
        # A load is in flight; a model/device change lands on the next ensure
        # after it settles (eventual consistency beats cancelling a download).
        return stt_status()
    _model = None
    _desired = desired
    _status = "loading"
    _error = None
    _load_task = asyncio.create_task(_load(desired))
    return stt_status()


async def wait_for_load() -> None:
    """Await the in-flight load, if any (tests + graceful verification)."""
    if _load_task is not None:
        await _load_task


def _transcribe_sync(model: Any, data: bytes) -> dict:
    """Runs in a worker thread. PyAV (a faster-whisper dependency) decodes the
    renderer's webm/opus container directly from the byte stream."""
    segments, info = model.transcribe(
        io.BytesIO(data), beam_size=STT_BEAM_SIZE, vad_filter=True
    )
    # `segments` is a lazy generator — joining it here IS the transcription.
    text = " ".join(seg.text.strip() for seg in segments).strip()
    return {
        "text": text,
        "language": getattr(info, "language", None),
        "duration": float(getattr(info, "duration", 0.0) or 0.0),
    }


async def transcribe_audio(
    data: bytes, model_name: str, device: str = "auto", compute_type: str = "auto"
) -> dict:
    """Transcribe one recorded utterance → {text, language, duration}.

    Not ready → kick the load (self-healing: a stray early call starts the
    download instead of failing inertly) and raise ModelNotReadyError for the
    API layer to surface as "still loading, try again shortly"."""
    desired = (model_name, device, compute_type)
    if _status != "ready" or _loaded != desired or _model is None:
        await ensure_model_loaded(model_name, device, compute_type)
        raise ModelNotReadyError(_status, _error or "")
    return await asyncio.to_thread(_transcribe_sync, _model, data)
