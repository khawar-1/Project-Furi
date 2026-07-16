"""
Jarvis OS — Context Layer API (Phase 8)

The endpoints the Electron sensing loops write to and the UI reads from. Config
lives in app/core/app_settings.py; the world model in app/core/context_store.py;
OCR in app/core/screen_ocr.py — this router only orchestrates and enforces the
privacy gates (the reminders-router rule: modules own their domain).

Every route is behind the app-wide AuthMiddleware. The privacy posture is
enforced HERE, structurally, not by prompt:
- The MASTER kill switch (ContextConfig.enabled) gates every write. A signal
  posted while sensing is off is accepted-and-ignored ({stored: false}); a
  screen frame posted while OCR is off is refused with 403 BEFORE any decode.
- Device signals arrive over ordinary authed HTTP POST — never the /ws socket,
  which stays strictly server→client.
- The raw screen frame is OCR'd in memory and dropped; only the condensed
  summary is retained (in the in-memory world model), never written to disk.
"""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.core.app_settings import (
    CONTEXT_MAX_IDLE_THRESHOLD,
    CONTEXT_MAX_OCR_INTERVAL,
    CONTEXT_MIN_IDLE_THRESHOLD,
    CONTEXT_MIN_OCR_INTERVAL,
    ContextConfig,
    get_context_config,
    set_context_config,
)
from app.core.context_store import (
    context_status,
    get_world_model,
    record_affective_signal,
    record_device_signal,
    record_ocr_summary,
)
from app.core.dependencies import get_db
from app.core.screen_ocr import condense_ocr_text, run_ocr

router = APIRouter()

#: The active app / window title are our own renderer's data, but still treated
#: as untrusted text: truncate before storing so a pathological title can never
#: bloat the in-memory model or a downstream prompt.
_APP_MAX = 256
_TITLE_MAX = 512

#: A downscaled screen thumbnail is well under this; the cap only refuses absurd
#: uploads before they reach the OCR engine.
MAX_FRAME_BYTES = 12 * 1024 * 1024


# --------------------------------------------------------------- settings

class ContextSettingsUpdate(BaseModel):
    enabled: bool
    device_sensing: bool = True
    screen_ocr: bool = False
    ocr_interval_seconds: int = 30
    idle_threshold_seconds: int = 300
    affective_sensing: bool = False


def _config_payload(config: ContextConfig) -> dict:
    return {
        "enabled": config.enabled,
        "device_sensing": config.device_sensing,
        "screen_ocr": config.screen_ocr,
        "ocr_interval_seconds": config.ocr_interval_seconds,
        "idle_threshold_seconds": config.idle_threshold_seconds,
        "affective_sensing": config.affective_sensing,
    }


@router.get("/settings", summary="Context/sensing settings")
async def get_settings(db=Depends(get_db)) -> dict:
    return _config_payload(await get_context_config(db))


@router.put("/settings", summary="Update context/sensing settings")
async def put_settings(update: ContextSettingsUpdate, db=Depends(get_db)) -> dict:
    if not (CONTEXT_MIN_OCR_INTERVAL <= update.ocr_interval_seconds <= CONTEXT_MAX_OCR_INTERVAL):
        raise HTTPException(
            status_code=400,
            detail=(
                f"ocr_interval_seconds must be {CONTEXT_MIN_OCR_INTERVAL}–"
                f"{CONTEXT_MAX_OCR_INTERVAL}."
            ),
        )
    if not (CONTEXT_MIN_IDLE_THRESHOLD <= update.idle_threshold_seconds <= CONTEXT_MAX_IDLE_THRESHOLD):
        raise HTTPException(
            status_code=400,
            detail=(
                f"idle_threshold_seconds must be {CONTEXT_MIN_IDLE_THRESHOLD}–"
                f"{CONTEXT_MAX_IDLE_THRESHOLD}."
            ),
        )
    await set_context_config(db, ContextConfig(
        enabled=update.enabled,
        device_sensing=update.device_sensing,
        screen_ocr=update.screen_ocr,
        ocr_interval_seconds=update.ocr_interval_seconds,
        idle_threshold_seconds=update.idle_threshold_seconds,
        affective_sensing=update.affective_sensing,
    ))
    return _config_payload(await get_context_config(db))


# ----------------------------------------------------------- device sensing

class DeviceSignal(BaseModel):
    active_app: str | None = None
    window_title: str | None = None
    idle_seconds: float | None = None


@router.post("/device", summary="Record a device signal (Electron main only)")
async def post_device(signal: DeviceSignal, db=Depends(get_db)) -> dict:
    """Store the latest active-app/window/idle signal. Gated: if the master
    switch or device sensing is off, the signal is accepted-and-ignored so the
    Electron poster never has to special-case a race with a just-flipped
    setting."""
    config = await get_context_config(db)
    if not config.enabled or not config.device_sensing:
        return {"stored": False}
    active_app = (signal.active_app or "").strip()[:_APP_MAX] or None
    window_title = (signal.window_title or "").strip()[:_TITLE_MAX] or None
    idle = signal.idle_seconds
    if idle is not None:
        try:
            idle = max(0.0, float(idle))
        except (TypeError, ValueError):
            idle = None
    record_device_signal(active_app, window_title, idle)
    return {"stored": True}


# ----------------------------------------------------- affective (Phase 13)

class AffectiveSignal(BaseModel):
    """A client-computed affective summary — TIMING and ENERGY only, never
    keystroke content or audio. Every field optional (a source not sensing now
    is simply absent). Values are clamped defensively before use."""
    typing_cpm: float | None = None
    backspace_rate: float | None = None
    voice_energy: float | None = None


def _clamp(value: float | None, lo: float, hi: float) -> float | None:
    if value is None:
        return None
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return None


@router.post("/state", summary="Record an affective summary (Electron/renderer only)")
async def post_state(signal: AffectiveSignal, db=Depends(get_db)) -> dict:
    """Store the latest typing-cadence / voice-energy summary. HARD-GATED: the
    master switch AND affective_sensing must both be on, else the signal is
    accepted-and-ignored ({stored: false}) so the poster never races a just-
    flipped setting (the /device convention). Only coarse numbers are accepted —
    there is no content channel here to leak."""
    config = await get_context_config(db)
    if not config.enabled or not config.affective_sensing:
        return {"stored": False}
    record_affective_signal(
        _clamp(signal.typing_cpm, 0.0, 2000.0),
        _clamp(signal.backspace_rate, 0.0, 1.0),
        _clamp(signal.voice_energy, 0.0, 1.0),
    )
    return {"stored": True}


# ------------------------------------------------------------- screen OCR

@router.post("/screen", summary="OCR one screen frame (hard-gated; Electron only)")
async def post_screen(file: UploadFile = File(...), db=Depends(get_db)) -> dict:
    """OCR a captured frame → a condensed on-screen-context summary, retained in
    the in-memory world model. HARD-GATED: 403 unless the master switch AND
    screen OCR are both on — checked before any decode. The raw image is never
    written to disk and is dropped as soon as OCR returns."""
    config = await get_context_config(db)
    if not config.enabled or not config.screen_ocr:
        raise HTTPException(
            status_code=403,
            detail="Screen sensing is disabled — enable it in Settings.",
        )
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded frame is empty.")
    if len(data) > MAX_FRAME_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Frame too large ({len(data)} bytes; max {MAX_FRAME_BYTES}).",
        )
    try:
        text = await run_ocr(data)
    except Exception as e:
        # An OCR engine hiccup is a transient failure, never a 500 that would
        # make the capture loop back off hard.
        raise HTTPException(status_code=400, detail=f"Could not read the screen: {e}")
    finally:
        del data  # drop the raw frame promptly; nothing persists it
    summary = condense_ocr_text(text)
    record_ocr_summary(summary)
    return {"stored": True, "summary": summary}


# --------------------------------------------------------------- reads

@router.get("/world", summary="The current world model (UI audit + Phase 9)")
async def get_world(db=Depends(get_db)) -> dict:
    model = await get_world_model(db)
    return model.to_dict()


@router.get("/status", summary="Cheap sensing status for the indicator (no I/O)")
async def get_status(db=Depends(get_db)) -> dict:
    return await context_status(db)
