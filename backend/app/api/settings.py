"""
Jarvis OS — Settings API (Phase 5, Part 6)

Runtime app settings the user toggles from the UI. Today that's the daily
briefing (on/off + time). Everything goes through app/core/app_settings.py and
app/core/daily_briefing.py; this router only orchestrates (the reminders-router
rule: routers orchestrate, modules own their domain).

The briefing PUT re-syncs the scheduler job in the same request, so turning it
on/off or changing the time takes effect immediately — no restart. run-now is
the manual "Send now" trigger (and the deterministic hook for the verify skill);
it is read-only end to end, so there is nothing to approve.
"""
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.app_settings import (
    VOICE_DEVICES,
    VOICE_SPOKEN_APPROVAL_LEVELS,
    VOICE_STT_COMPUTE_TYPES,
    VOICE_STT_MODELS,
    VOICE_TTS_VOICE_IDS,
    VOICE_TTS_VOICES,
    BriefingConfig,
    VoiceConfig,
    get_briefing_config,
    get_briefing_job_id,
    get_voice_config,
    set_briefing_config,
    set_voice_config,
)
from app.core.daily_briefing import run_briefing_now, sync_briefing_job
from app.core.dependencies import get_db
from app.core.scheduler import scheduler
from app.core.voice_stt import ensure_model_loaded, stt_status
from app.core.voice_tts import ensure_engine_loaded, tts_status

router = APIRouter()

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class BriefingUpdate(BaseModel):
    enabled: bool
    time: str  # "HH:MM" local


async def _briefing_payload(db) -> dict:
    """The GET/PUT response shape: config + the next scheduled run (from the
    live pending job, so the UI shows the real timer, not just the config)."""
    config = await get_briefing_config(db)
    next_run_at = None
    job_id = await get_briefing_job_id(db)
    if job_id:
        jobs = await scheduler.list_jobs(status="pending", limit=10_000)
        row = next((j for j in jobs if j["id"] == job_id), None)
        if row is not None:
            next_run_at = row["run_at"]  # already utc_iso from list_jobs
    return {
        "enabled": config.enabled,
        "time": config.time_str,
        "next_run_at": next_run_at,
    }


@router.get("/briefing", summary="Daily briefing settings")
async def get_briefing(db=Depends(get_db)) -> dict:
    return await _briefing_payload(db)


@router.put("/briefing", summary="Update daily briefing settings")
async def put_briefing(update: BriefingUpdate, db=Depends(get_db)) -> dict:
    match = _TIME_RE.match(update.time.strip())
    if not match:
        raise HTTPException(
            status_code=400,
            detail="time must be 24-hour 'HH:MM' (00:00–23:59), e.g. 08:00",
        )
    hour, minute = int(match.group(1)), int(match.group(2))
    await set_briefing_config(db, BriefingConfig(enabled=update.enabled, hour=hour, minute=minute))
    # Re-sync the scheduler job in the same request — the change takes effect now.
    await sync_briefing_job(db)
    return await _briefing_payload(db)


@router.post("/briefing/run-now", summary="Compose and deliver a briefing immediately")
async def post_briefing_run_now(db=Depends(get_db)) -> dict:
    body = await run_briefing_now(db)
    return {"delivered": True, "message": body}


# ------------------------------------------------------ voice (Phase 7, Part 1)


class VoiceUpdate(BaseModel):
    enabled: bool
    stt_model: str = "small"
    review_before_send: bool = False
    # Part 3 output fields — defaulted so a Part-2-shaped PUT stays valid.
    output_enabled: bool = True
    # Kokoro preset voice id (validated against VOICE_TTS_VOICE_IDS).
    voice: str = "af_heart"
    speak_proactive: bool = False
    speak_all_responses: bool = False
    # Part 5 — defaulted so a Part-3/4-shaped PUT stays valid.
    listen_on_summon: bool = False
    # Speaking speed (1.0 = natural) — defaulted so an older-shaped PUT stays valid.
    tts_speed: float = 1.0
    # GPU voice round — device selection (auto/cpu/cuda) + whisper precision;
    # defaulted so an older-shaped PUT stays valid.
    stt_device: str = "auto"
    tts_device: str = "auto"
    stt_compute_type: str = "auto"
    # Phase 12 ambient fields — defaulted so an older-shaped PUT stays valid.
    continuous_conversation: bool = False
    wake_word: bool = False
    # How far spoken consent may go: off | write | all. Defaulted to the
    # SAFE end so an older-shaped PUT can never widen it by omission.
    spoken_approval: str = "off"


async def _voice_payload(db) -> dict:
    """Config + the live model state + the preset-voice list, so the settings
    card renders one fetch. stt_status()/tts_status() are purely local (no I/O)
    — safe on every GET."""
    config = await get_voice_config(db)
    return {
        "enabled": config.enabled,
        "stt_model": config.stt_model,
        "review_before_send": config.review_before_send,
        "output_enabled": config.output_enabled,
        "voice": config.voice,
        "speak_proactive": config.speak_proactive,
        "speak_all_responses": config.speak_all_responses,
        "listen_on_summon": config.listen_on_summon,
        "tts_speed": config.tts_speed,
        "stt_device": config.stt_device,
        "tts_device": config.tts_device,
        "stt_compute_type": config.stt_compute_type,
        "continuous_conversation": config.continuous_conversation,
        "wake_word": config.wake_word,
        "spoken_approval": config.spoken_approval,
        "stt_models": list(VOICE_STT_MODELS),
        "voices": [{"id": vid, "label": label} for vid, label in VOICE_TTS_VOICES],
        "devices": list(VOICE_DEVICES),
        "stt_compute_types": list(VOICE_STT_COMPUTE_TYPES),
        "spoken_approval_levels": list(VOICE_SPOKEN_APPROVAL_LEVELS),
        "stt_status": stt_status(),
        "tts_status": tts_status(),
    }


@router.get("/voice", summary="Voice settings")
async def get_voice(db=Depends(get_db)) -> dict:
    return await _voice_payload(db)


@router.put("/voice", summary="Update voice settings")
async def put_voice(update: VoiceUpdate, db=Depends(get_db)) -> dict:
    if update.stt_model not in VOICE_STT_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"stt_model must be one of: {', '.join(VOICE_STT_MODELS)}",
        )
    # `voice` is a Kokoro preset id. An unknown id falls back to the default
    # rather than 400 (forward/backward compatibility — the picker and the
    # whitelist should never disagree, but be lenient if they do).
    voice = update.voice if update.voice in VOICE_TTS_VOICE_IDS else "af_heart"
    # Device/compute are lenient too — an out-of-whitelist value falls back to
    # "auto" rather than 400 (set_voice_config re-validates via _coerce_voice).
    stt_device = update.stt_device if update.stt_device in VOICE_DEVICES else "auto"
    tts_device = update.tts_device if update.tts_device in VOICE_DEVICES else "auto"
    stt_compute = (
        update.stt_compute_type
        if update.stt_compute_type in VOICE_STT_COMPUTE_TYPES
        else "auto"
    )
    # An unknown level falls back to "off" — the safe end, matching the
    # coercer. Consent settings never fail open.
    spoken_approval = (
        update.spoken_approval
        if update.spoken_approval in VOICE_SPOKEN_APPROVAL_LEVELS
        else "off"
    )
    await set_voice_config(db, VoiceConfig(
        enabled=update.enabled,
        stt_model=update.stt_model,
        review_before_send=update.review_before_send,
        output_enabled=update.output_enabled,
        voice=voice,
        speak_proactive=update.speak_proactive,
        speak_all_responses=update.speak_all_responses,
        listen_on_summon=update.listen_on_summon,
        tts_speed=update.tts_speed,
        stt_device=stt_device,
        tts_device=tts_device,
        stt_compute_type=stt_compute,
        continuous_conversation=update.continuous_conversation,
        wake_word=update.wake_word,
        spoken_approval=spoken_approval,
    ))
    # Enabling (or switching device) kicks the model load NOW — the FileIndexCard
    # enable-flow lesson: a toggle that silently does nothing until some later
    # trigger is a recorded live-bug class. A device change reloads the engine
    # (ensure_* is keyed on device); switching preset voices needs no reload.
    # ensure returns immediately; the card polls /api/voice/status through it.
    if update.enabled:
        await ensure_model_loaded(update.stt_model, stt_device, stt_compute)
        if update.output_enabled:
            await ensure_engine_loaded(tts_device)
    return await _voice_payload(db)
