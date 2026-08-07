"""
Jarvis OS — CUDA runtime bootstrap (Phase 7 voice-speed round)

Both voice engines can run on the machine's GPU: faster-whisper (STT) through
ctranslate2's CUDA backend, and Kokoro (TTS) through onnxruntime-gpu's
CUDAExecutionProvider. Neither ships the CUDA math libraries — they load
cuDNN 9 / cuBLAS 12 / the CUDA runtime as DLLs at inference time. We install
those as venv wheels (`nvidia-cudnn-cu12`, `nvidia-cublas-cu12`,
`nvidia-cuda-runtime-cu12`) rather than depending on a system CUDA toolkit, so
the setup is reproducible — but the wheels drop their DLLs under
`site-packages/nvidia/<lib>/bin`, which Windows' loader does NOT search by
default.

`register_cuda_dll_dirs()` adds those bin dirs to the DLL search path (via
`os.add_dll_directory`) so both engines can resolve the libraries. It runs ONCE,
at the very top of the backend lifespan (before any voice/onnxruntime import),
and again defensively inside each engine factory — idempotent, best-effort, and
a complete no-op off Windows / when the wheels aren't installed (the CPU path
then stays the only option, which is exactly the graceful fallback we want).

`cuda_available()` reports whether a usable CUDA device is present, so the
engine factories can auto-select the device without importing torch (the whole
reason the voice stack is torch-free — see voice_tts.py's module docstring).
"""
import glob
import os
import sys
from typing import Optional

from loguru import logger

#: The CUDA math-library wheels whose `bin` dirs hold the DLLs the engines need.
#: cuDNN 9 serves BOTH ctranslate2 (STT) and onnxruntime-gpu (TTS).
_NVIDIA_LIB_DIRS = ("cudnn", "cublas", "cuda_runtime", "cuda_nvrtc")

#: A system CUDA toolkit supplies cuBLAS + the CUDA runtime (cublas64_12.dll /
#: cudart64_12.dll) when the (large, flaky-to-download) pip wheels aren't
#: installed. We register its bin dir too, so the engines resolve those DLLs
#: from whichever source is present — wheel or toolkit. Newest toolkit first.
_CUDA_TOOLKIT_GLOB = os.path.join(
    os.environ.get("ProgramFiles", r"C:\Program Files"),
    "NVIDIA GPU Computing Toolkit",
    "CUDA",
    "v*",
    "bin",
)

_registered = False
_cuda_available: Optional[bool] = None


def register_cuda_dll_dirs() -> list[str]:
    """Add each installed `nvidia/<lib>/bin` dir to the DLL search path.

    Idempotent (runs its work once) and best-effort — any failure is logged and
    swallowed, leaving the CPU path intact. Returns the dirs that were added
    (empty on non-Windows or when no wheels are present)."""
    global _registered
    if _registered:
        return []
    _registered = True
    if sys.platform != "win32":
        return []  # Linux/mac resolve wheel DLLs via RPATH; nothing to do.

    candidates: list[str] = []

    # 1. Per-library pip wheels: site-packages/nvidia/<lib>/bin
    try:
        import nvidia  # the namespace package the CUDA wheels install into

        for root in getattr(nvidia, "__path__", []):
            for lib in _NVIDIA_LIB_DIRS:
                candidates.append(os.path.join(root, lib, "bin"))
    except Exception:
        logger.debug("No 'nvidia' CUDA wheels found; will try a system toolkit.")

    # 2. A system CUDA toolkit (supplies cuBLAS + cudart when the wheels aren't
    #    installed). Newest version dir first.
    candidates.extend(sorted(glob.glob(_CUDA_TOOLKIT_GLOB), reverse=True))

    added: list[str] = []
    for bin_dir in candidates:
        if os.path.isdir(bin_dir):
            try:
                os.add_dll_directory(bin_dir)
                # ALSO prepend to PATH: os.add_dll_directory alone does not
                # reliably cover a runtime-loaded DLL's TRANSITIVE dependency
                # resolution on Windows (onnxruntime loads its provider DLL,
                # which pulls cudnn/cublas — those resolve via PATH). Both
                # together is the belt-and-suspenders that actually works.
                if bin_dir not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
                added.append(bin_dir)
            except OSError as e:  # pragma: no cover - environment dependent
                logger.debug(f"Could not add CUDA DLL dir '{bin_dir}': {e}")
    if added:
        logger.info(f"Registered {len(added)} CUDA DLL dir(s) for GPU voice engines.")
    else:
        logger.debug("No CUDA DLL dirs found; voice engines will use CPU.")
    return added


def cuda_available() -> bool:
    """True when a usable CUDA device is present (result cached).

    Uses ctranslate2's device probe — it's always installed (faster-whisper's
    engine) and needs no torch. Best-effort: any failure reads as 'no GPU', so
    the engine factories fall back to CPU. Call `register_cuda_dll_dirs()` first
    so the probe can load the CUDA driver libraries."""
    global _cuda_available
    if _cuda_available is not None:
        return _cuda_available
    register_cuda_dll_dirs()
    try:
        import ctranslate2

        _cuda_available = ctranslate2.get_cuda_device_count() > 0
    except Exception as e:
        logger.debug(f"CUDA probe failed (using CPU): {e}")
        _cuda_available = False
    return _cuda_available


def reset_cuda_probe() -> None:
    """Test hook: forget the cached probe result (not the DLL registration)."""
    global _cuda_available
    _cuda_available = None


#: Logical cores deliberately left to the rest of the machine. Jarvis is a
#: desktop app sharing a laptop with the user's real work, not a batch job that
#: owns the box.
_RESERVED_CORES = 2


def cpu_worker_threads() -> int:
    """How many threads a local inference engine may use on the CPU path.

    WHY THIS EXISTS. voice_stt and voice_tts each asked for `os.cpu_count()` —
    on this machine 16 — so a single transcription or one spoken sentence could
    claim every logical core. Worse, they reach the CPU path by SILENT FALLBACK:
    a CUDA init failure is caught and retried on CPU, so a VRAM squeeze on a
    6 GB laptop GPU converts itself into a whole-machine CPU saturation event,
    which is what "the laptop gets stuck" actually is.

    `os.cpu_count()` also returns LOGICAL processors, while voice_stt's comment
    claimed it was using physical cores — 2x its own stated intent on any SMT
    part. Hyper-threads share execution resources, so for compute-bound
    inference the second thread on a core buys little and costs contention.

    So: half the logical count (a reasonable stand-in for physical cores with no
    new dependency), minus headroom, floored at 1. On 16 logical -> 6.

    This bounds ENGINE threads only. It is not a global cap: the shared
    asyncio.to_thread executor and fastembed are untouched.
    """
    logical = os.cpu_count() or 4
    physical_ish = max(1, logical // 2)
    return max(1, physical_ish - _RESERVED_CORES) if physical_ish > _RESERVED_CORES else max(1, physical_ish)
