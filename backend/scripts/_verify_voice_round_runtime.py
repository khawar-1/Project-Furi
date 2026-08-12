"""RUNTIME verification of the 2026-08-12 voice round's backend half.

Boots the REAL `main.py` lifespan on an isolated port against a scratch database
and drives the real HTTP surface. A hermetic test cannot tell you the wiring
boots — this project has recorded that lesson enough times to write the probe
every round.

Voice is deliberately left DISABLED throughout: this verifies the config
plumbing (the collapsed validation path, the asdict payload, the new fields),
and enabling would download ~1.5 GB of models onto a machine that is short of
VRAM for no extra evidence.

    cd backend && venv\\Scripts\\python scripts\\_verify_voice_round_runtime.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

SCRATCH = Path(tempfile.mkdtemp(prefix="jarvis-voice-verify-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{(SCRATCH / 'scratch.db').as_posix()}"
os.environ["BACKEND_PORT"] = "18005"
os.environ["REMOTE_ENABLED"] = "false"

import asyncio  # noqa: E402

import httpx  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}{f'  — {detail}' if detail else ''}")


async def main() -> None:
    from main import app

    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        from app.core import auth

        headers = (
            {"X-Jarvis-Token": auth.get_or_create_token()} if auth.ENABLED else {}
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers=headers
        ) as client:
            print("\n--- GET /api/settings/voice (a database that has never seen voice)")
            r = await client.get("/api/settings/voice")
            check("200", r.status_code == 200, str(r.status_code))
            body = r.json()
            check("stt_language defaults to English, NOT auto-detect",
                  body.get("stt_language") == "en", repr(body.get("stt_language")))
            check("wake_mode defaults to speech", body.get("wake_mode") == "speech",
                  repr(body.get("wake_mode")))
            check("wake_phrase defaults to furi", body.get("wake_phrase") == "furi",
                  repr(body.get("wake_phrase")))
            check("the language choices are offered to the UI",
                  "auto" in body.get("stt_languages", []) and "en" in body.get("stt_languages", []))
            check("the wake modes are offered to the UI",
                  body.get("wake_modes") == ["speech", "model"], repr(body.get("wake_modes")))
            # The payload is now asdict(config) — every dataclass field must be
            # present, which is what the hand-listed version silently lost.
            from dataclasses import fields

            from app.core.app_settings import VoiceConfig

            missing = [f.name for f in fields(VoiceConfig) if f.name not in body]
            check("EVERY VoiceConfig field reaches the UI", not missing, f"missing {missing}")

            print("\n--- PUT round-trip (the collapsed single validation path)")
            put = {
                "enabled": False,
                "stt_model": "small",
                "stt_language": "ur",
                "wake_word": True,
                "wake_mode": "model",
                "wake_phrase": "hey furi",
                "voice": "bm_george",
                "tts_speed": 1.25,
            }
            r = await client.put("/api/settings/voice", json=put)
            check("200", r.status_code == 200, r.text[:200])
            body = r.json()
            check("language persisted", body.get("stt_language") == "ur", repr(body.get("stt_language")))
            check("wake mode persisted", body.get("wake_mode") == "model")
            check("wake phrase persisted", body.get("wake_phrase") == "hey furi")
            check("an omitted field takes its default, not junk",
                  body.get("spoken_approval") == "off", repr(body.get("spoken_approval")))
            r = await client.get("/api/settings/voice")
            check("survives a re-read", r.json().get("wake_phrase") == "hey furi")

            print("\n--- the coercer refuses junk over HTTP (never 500s)")
            r = await client.put(
                "/api/settings/voice",
                json={**put, "stt_language": "klingon", "wake_mode": "telepathy",
                      "wake_phrase": "DROP TABLE users;--"},
            )
            check("200 rather than a crash", r.status_code == 200, str(r.status_code))
            body = r.json()
            check("bad language → en", body.get("stt_language") == "en", repr(body.get("stt_language")))
            check("bad wake mode → speech", body.get("wake_mode") == "speech")
            check("bad phrase → furi", body.get("wake_phrase") == "furi", repr(body.get("wake_phrase")))

            print("\n--- an OLD-shaped PUT (no new fields at all) stays valid")
            r = await client.put(
                "/api/settings/voice",
                json={"enabled": False, "stt_model": "small", "voice": "af_heart"},
            )
            check("200", r.status_code == 200, r.text[:200])
            check("defaults fill in", r.json().get("stt_language") == "en")

            print("\n--- transcribe still refuses cleanly while voice is off")
            r = await client.post(
                "/api/voice/transcribe", files={"file": ("a.wav", b"x", "audio/wav")}
            )
            check("400 with a pointer to Settings", r.status_code == 400, str(r.status_code))

    print(f"\n{'PASS' if FAIL == 0 else 'FAIL'} — {PASS}/{PASS + FAIL} checks")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
