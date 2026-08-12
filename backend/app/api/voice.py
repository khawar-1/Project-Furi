"""
Furi OS — Voice API (Phase 7, Parts 1 + 3)

The runtime voice endpoints: transcribe one push-to-talk utterance, speak one
piece of text, and the model status the UI polls while the (large, opt-in)
model downloads run. Config CRUD lives in /api/settings/voice
(app/api/settings.py) per the settings-router convention; this router only
orchestrates — the model lifecycles are app/core/voice_stt.py's and
app/core/voice_tts.py's domain.

Audio never leaves the machine: the renderer posts recorded bytes to this
loopback-only backend (faster-whisper decodes locally) and receives Kokoro's
WAV bytes back from it.
"""
from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel

from app.core.app_settings import get_voice_config
from app.core.dependencies import get_db
from app.core.voice_stt import ModelNotReadyError, stt_status, transcribe_audio
from app.core.voice_tts import (
    TtsNotReadyError,
    engine_sample_rate,
    sanitize_for_speech,
    synthesize_speech,
    synthesize_speech_stream,
    tts_status,
)

router = APIRouter()

#: A 60s webm/opus utterance is ~1MB — this cap only exists to refuse absurd
#: uploads before they reach the decoder.
MAX_AUDIO_BYTES = 20 * 1024 * 1024

#: Speech is synthesized sentence-by-sentence (Part 4's segmenter) — this cap
#: only exists to refuse absurd bodies before they reach the engine.
MAX_SPEAK_CHARS = 2000


@router.get("/status", summary="Voice runtime status (poll while a model downloads)")
async def get_status(db=Depends(get_db)) -> dict:
    config = await get_voice_config(db)
    return {
        "enabled": config.enabled,
        "stt": {**stt_status(), "configured_model": config.stt_model},
        "tts": {
            **tts_status(),
            "configured_voice": config.voice,
            "output_enabled": config.output_enabled,
        },
    }


@router.post("/transcribe", summary="Transcribe one recorded utterance")
async def post_transcribe(file: UploadFile = File(...), db=Depends(get_db)) -> dict:
    config = await get_voice_config(db)
    if not config.enabled:
        raise HTTPException(
            status_code=400,
            detail="Voice input is disabled — enable it in Settings.",
        )
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded audio is empty.")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Audio too large ({len(data)} bytes; max {MAX_AUDIO_BYTES}).",
        )
    try:
        return await transcribe_audio(
            data,
            model_name=config.stt_model,
            device=config.stt_device,
            compute_type=config.stt_compute_type,
            language=config.stt_language,
        )
    except ModelNotReadyError as e:
        # 409, not 400: the request was fine — the model just isn't there yet.
        # transcribe_audio already kicked the load; the UI polls /status.
        raise HTTPException(
            status_code=409,
            detail=f"The transcription model is not ready yet (status: {e.status})."
            + (f" {e.detail}" if e.detail else " Try again shortly."),
        )
    except Exception as e:
        # Undecodable/corrupt audio and the like — a bad request, not a crash.
        raise HTTPException(status_code=400, detail=f"Could not transcribe audio: {e}")


class SpeakRequest(BaseModel):
    text: str
    #: Opt OUT of the markdown→speech sanitizer (callers that already hold
    #: plain speakable text). The default is always sanitized — the engine never
    #: reads raw markdown aloud.
    raw: bool = False


@router.post("/speak", summary="Synthesize one piece of text to WAV")
async def post_speak(request: SpeakRequest, db=Depends(get_db)) -> Response:
    config = await get_voice_config(db)
    if not config.enabled:
        raise HTTPException(
            status_code=400,
            detail="Voice is disabled — enable it in Settings.",
        )
    if not config.output_enabled:
        raise HTTPException(
            status_code=400,
            detail="Voice output is disabled — enable it in Settings.",
        )
    text = request.text or ""
    if not text.strip():
        raise HTTPException(status_code=400, detail="There is no text to speak.")
    if len(text) > MAX_SPEAK_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Text too long ({len(text)} chars; max {MAX_SPEAK_CHARS}).",
        )
    if not request.raw:
        text = sanitize_for_speech(text)
        if not text:
            # Sanitized to nothing (pure markdown scaffolding / emoji) — an
            # outcome, not an error; the playback queue simply skips it.
            return Response(status_code=204)
    try:
        wav = await synthesize_speech(
            text, voice=config.voice, speed=config.tts_speed, device=config.tts_device
        )
    except TtsNotReadyError as e:
        # 409, not 400: the request was fine — the engine just isn't there
        # yet. synthesize_speech already kicked the load; the UI polls /status.
        raise HTTPException(
            status_code=409,
            detail=f"The speech engine is not ready yet (status: {e.status})."
            + (f" {e.detail}" if e.detail else " Try again shortly."),
        )
    except Exception as e:
        # A synthesis failure is a bad request/transient engine hiccup — the
        # queue skips the sentence; never a 500.
        raise HTTPException(status_code=400, detail=f"Could not synthesize speech: {e}")
    return Response(content=wav, media_type="audio/wav")


@router.post("/speak/stream", summary="Synthesize text to a live PCM stream")
async def post_speak_stream(request: SpeakRequest, db=Depends(get_db)):
    """The /speak contract, but audio starts flowing while the sentence is
    still being synthesized: raw PCM16 (s16le mono) chunks at the sample rate
    named by the X-Sample-Rate header. The playback side schedules the chunks
    with Web Audio; a client that aborts mid-stream stops the synthesis within
    one decode step (voice_tts's stop event)."""
    config = await get_voice_config(db)
    if not config.enabled:
        raise HTTPException(
            status_code=400,
            detail="Voice is disabled — enable it in Settings.",
        )
    if not config.output_enabled:
        raise HTTPException(
            status_code=400,
            detail="Voice output is disabled — enable it in Settings.",
        )
    text = request.text or ""
    if not text.strip():
        raise HTTPException(status_code=400, detail="There is no text to speak.")
    if len(text) > MAX_SPEAK_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Text too long ({len(text)} chars; max {MAX_SPEAK_CHARS}).",
        )
    if not request.raw:
        text = sanitize_for_speech(text)
        if not text:
            return Response(status_code=204)

    # Prime the generator BEFORE the streaming response starts: an async
    # generator raises on its first __anext__, which would otherwise land
    # AFTER the 200 headers went out — not-ready must still be an honest 409.
    stream = synthesize_speech_stream(
        text, voice=config.voice, speed=config.tts_speed, device=config.tts_device
    )
    try:
        first_chunk = await stream.__anext__()
    except StopAsyncIteration:
        return Response(status_code=204)  # nothing synthesizable
    except TtsNotReadyError as e:
        raise HTTPException(
            status_code=409,
            detail=f"The speech engine is not ready yet (status: {e.status})."
            + (f" {e.detail}" if e.detail else " Try again shortly."),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not synthesize speech: {e}")

    async def body():
        try:
            yield first_chunk
            async for chunk in stream:
                yield chunk
        except Exception as e:
            # Headers are long gone — end the stream truncated, never a 500
            # mid-body. The playback side treats a short stream as a short
            # utterance and moves on.
            logger.warning(f"Speech stream ended early ({type(e).__name__}: {e})")
        finally:
            await stream.aclose()

    return StreamingResponse(
        body(),
        media_type="application/octet-stream",
        headers={
            "X-Sample-Rate": str(engine_sample_rate()),
            "X-Audio-Format": "pcm_s16le",
            "Cache-Control": "no-store",
        },
    )
