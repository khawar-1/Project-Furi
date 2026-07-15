"""
Jarvis OS — Voice speed benchmark (STT + TTS, CPU vs GPU).

Loads the REAL Whisper (ctranslate2) and Kokoro (onnxruntime) engines through
the app's own builders and times a warm synth + a warm transcribe on each
device, so you can see the GPU win directly. Downloads the models on first run.

Run from backend/:  venv\\Scripts\\python scripts\\bench_voice.py
NEVER collected by pytest (lives outside tests/, needs real models + a GPU).
"""
import io
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.gpu_bootstrap import cuda_available, register_cuda_dll_dirs

register_cuda_dll_dirs()

SENTENCE = (
    "Good evening. I've finished indexing your files and your calendar is clear "
    "until three o'clock this afternoon."
)
STT_MODEL = "small"


def _bench_device(device: str) -> None:
    print(f"\n=== device: {device} ===")

    # --- TTS: build Kokoro + warm + time one synth ------------------------
    from app.core import voice_tts

    model_path, voices_path = voice_tts._ensure_model_files()
    voice_tts._prepare_espeak()
    t = time.perf_counter()
    kmodel, tts_actual = voice_tts._build_kokoro(model_path, voices_path, device)
    engine = voice_tts._KokoroEngine(kmodel, voice_tts.DEFAULT_SR)
    engine.synthesize("Warmup.", voice_tts.DEFAULT_VOICE, 1.0)  # warm
    print(f"  TTS load+warm ({tts_actual}): {time.perf_counter() - t:.1f}s")

    t = time.perf_counter()
    wav = engine.synthesize(SENTENCE, voice_tts.DEFAULT_VOICE, 1.0)
    tts_ms = (time.perf_counter() - t) * 1000
    # audio duration for a real-time-factor readout
    with wave.open(io.BytesIO(wav)) as wf:
        audio_s = wf.getnframes() / wf.getframerate()
    print(f"  TTS synth (warm): {tts_ms:.0f} ms  for {audio_s:.1f}s audio  "
          f"(RTF {tts_ms / 1000 / audio_s:.2f})")

    # --- STT: build Whisper + warm + time one transcribe -----------------
    from app.core import voice_stt

    dev = voice_stt._resolve_device(device)
    compute = voice_stt._resolve_compute_type(dev, "auto")
    t = time.perf_counter()
    wmodel = voice_stt._build_model(STT_MODEL, dev, compute)
    voice_stt._warmup(wmodel)
    print(f"  STT load+warm ({dev}/{compute}): {time.perf_counter() - t:.1f}s")

    t = time.perf_counter()
    res = voice_stt._transcribe_sync(wmodel, wav)  # transcribe the TTS output
    stt_ms = (time.perf_counter() - t) * 1000
    print(f"  STT transcribe (warm): {stt_ms:.0f} ms  for {audio_s:.1f}s audio  "
          f"(RTF {stt_ms / 1000 / audio_s:.2f})")
    print(f"  round-trip transcript: {res['text']!r}")


def main() -> None:
    print(f"CUDA available: {cuda_available()}")
    if cuda_available():
        _bench_device("cuda")
    _bench_device("cpu")


if __name__ == "__main__":
    main()
