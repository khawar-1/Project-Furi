"""Runtime verification for the magic-word round (2026-08-06).

A hermetic test cannot tell you the wiring BOOTS. This drives the REAL main.py
lifespan on an isolated port with a scratch DB and a scripted provider, so the
whole chain is exercised end to end: the gate, the classifier retry, the
catalog, the dead-end guard, the rescue, and the routing_decisions row that
made this round diagnosable in the first place.

⚠️ EVERYTHING RUNS IN ONE PROCESS/INVOCATION. The 2026-08-03 lesson: a probe
that stashed its token between shell invocations sent every request with an
EMPTY token, got 401 on everything, and "all denied" read exactly like success.

⚠️ THE PROVIDER IS SCRIPTED, NOT REAL. The defect under test is what the code
does when the model returns nothing — which a real provider will not do on
demand. The one thing a real provider is needed for (does the model actually
route "open my downloads folder" now that the catalog names it?) is called out
at the end as user-driven, not faked here.

Run:  venv\\Scripts\\python scripts\\_verify_magic_words_runtime.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import AsyncIterator, List, Optional

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
API_PORT = 18004

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), label))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  — {detail}" if detail else ""))


async def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="jarvis-magicword-verify-"))
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{scratch / 'scratch.db'}"
    os.environ["QDRANT_HOST"] = ""
    os.environ["BACKEND_PORT"] = str(API_PORT)
    os.environ["REMOTE_ENABLED"] = "false"
    os.environ["DEBUG"] = "false"
    sys.path.insert(0, str(ROOT))

    import uvicorn
    from main import app
    from app.core.auth import get_or_create_token
    from app.core.dependencies import get_llm_provider
    from app.providers.base import (
        EmbeddingResponse, LLMMessage, LLMProvider, LLMResponse,
    )

    demo = scratch / "fomi"
    demo.mkdir()

    class Scripted(LLMProvider):
        def __init__(self) -> None:
            self.responses: List[str] = []
            self.streams: List[str] = []
            self.chat_calls = 0
            self.stream_calls = 0
            self.prompts: List[str] = []

        @property
        def provider_name(self) -> str: return "scripted"

        @property
        def model_name(self) -> str: return "scripted-model"

        async def chat(self, messages, temperature=0.7, max_tokens=None):
            self.chat_calls += 1
            self.prompts.append(messages[0].content)
            content = self.responses.pop(0) if self.responses else "CHAT"
            return LLMResponse(content=content, model="m", provider="scripted")

        async def stream_chat(self, messages, temperature=0.7, max_tokens=None):
            self.stream_calls += 1
            text = self.streams.pop(0) if self.streams else "..."
            for word in text.split(" "):
                yield word + " "

        async def embed(self, text: str) -> EmbeddingResponse:
            return EmbeddingResponse(embedding=[0.0] * 384, model="m", provider="scripted")

    provider = Scripted()
    app.dependency_overrides[get_llm_provider] = lambda: provider

    config = uvicorn.Config(app, host="127.0.0.1", port=API_PORT, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(300):
        if server.started:
            break
        await asyncio.sleep(0.1)
    if not server.started:
        print("backend never started")
        return 1
    print(f"real backend lifespan booted on :{API_PORT}\n")

    import httpx
    from sqlalchemy import select
    from app.db.database import AsyncSessionLocal
    from app.db.models import RoutingDecision

    headers = {"X-Jarvis-Token": get_or_create_token()}
    base = f"http://127.0.0.1:{API_PORT}"

    def sse(body: str) -> list[dict]:
        return [json.loads(l[6:]) for l in body.splitlines() if l.startswith("data: ")]

    def text_of(events) -> str:
        return "".join(e.get("delta", "") for e in events if e.get("type") != "plan")

    def plans_of(events) -> list:
        return [e for e in events if e.get("type") == "plan"]

    async def say(http, message: str, session: str) -> list[dict]:
        r = await http.post(f"{base}/chat/stream", json={
            "messages": [{"role": "user", "content": message}], "session_id": session,
        })
        assert r.status_code == 200, r.text
        return sse(r.text)

    async def row_for(session: str):
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(RoutingDecision).where(RoutingDecision.session_id == session)
            )).scalars().all()
            return rows[-1] if rows else None

    try:
        async with httpx.AsyncClient(headers=headers, timeout=60) as http:
            plan = json.dumps({
                "steps": [{"description": "open the folder", "tool": "open_folder",
                           "parameters": {"path": str(demo)}}],
                "unachievable_reason": None, "question": None,
            })

            # -- ⚠️ THE INCIDENT, over real HTTP -------------------------------
            provider.responses = ["", "TASK INLINE", plan, plan]
            provider.streams = ["chat must never answer this turn"]
            provider.chat_calls = 0
            provider.stream_calls = 0
            events = await say(http, "furi open fomi folder", "rt-incident")

            check(provider.chat_calls >= 2,
                  "AN EMPTY CLASSIFIER REPLY IS RETRIED, not taken as chat",
                  f"{provider.chat_calls} chat calls")
            check(provider.stream_calls == 0,
                  "the turn routed, so the chat model never answered it")
            got = plans_of(events)
            check(bool(got) and got[0]["plan"]["status"] == "awaiting_approval",
                  "it reached a real approval card for the real folder",
                  got[0]["plan"]["status"] if got else "(no plan)")
            body = text_of(events).lower()
            check("direct instruction" not in body and "say it as" not in body,
                  "NO MAGIC-WORD DEMAND anywhere in what the user sees")

            # -- the audit row tells the story ---------------------------------
            row = await row_for("rt-incident")
            check(row is not None and row.gate_fired is True
                  and row.gate_reason == "strong_domain",
                  "the routing row records the gate firing as it always did",
                  f"{row.gate_reason if row else '(none)'}")
            # ⚠️ `fail_open_reason` is "" when unset, NOT None (models.py gives
            # the column default=""). The first cut of this probe asserted
            # `is None`, went red, and read exactly like a code defect — the
            # recorded rule that a probe asserting the wrong thing manufactures
            # defects, hit for the third time in this project.
            check(row is not None and not row.fail_open_reason
                  and row.label == "TASK",
                  "and a RECOVERED retry is not recorded as a fail-open",
                  f"label={row.label if row else '?'} "
                  f"fail_open={(row.fail_open_reason if row else '?')!r}")

            # -- ⚠️ the residual: routing genuinely says CHAT ------------------
            provider.responses = ["CHAT", plan, plan]
            provider.streams = [
                "I can open it for you, sir. Say it as one direct instruction, "
                "e.g. \"open the fomi folder\"."
            ]
            provider.chat_calls = 0
            provider.stream_calls = 0
            events = await say(http, "furi open fomi folder", "rt-rescue")

            got = plans_of(events)
            check(bool(got),
                  "A NON-WEB DEAD END IS RESCUED into a real plan",
                  got[0]["plan"]["status"] if got else "(no plan)")
            body = text_of(events).lower()
            check("say it as" not in body and "direct instruction" not in body,
                  "the demand was cut before it reached the screen")
            check("i can open it for you" in body,
                  "the honest half of the sentence still stands")
            row = await row_for("rt-rescue")
            check(row is not None and row.rescue_fired is True,
                  "the rescue is recorded as the routing miss it is")

            # -- a clean CHAT still costs exactly one call ---------------------
            provider.responses = ["CHAT"]
            provider.streams = ["Nothing was deleted, sir."]
            provider.chat_calls = 0
            provider.stream_calls = 0
            await say(http, "i finally cleaned up my temp files", "rt-chat")
            check(provider.chat_calls == 1,
                  "a CHAT judgement is never retried — conversation stays cheap",
                  f"{provider.chat_calls} chat call(s)")

            # -- the catalog the live model will actually read -----------------
            provider.responses = ["CHAT"]
            provider.streams = ["Of course, sir."]
            provider.prompts = []
            await say(http, "open my downloads folder", "rt-catalog")
            # ⚠️ NOT prompts[0]. Background memory extraction from the PREVIOUS
            # chat turn also calls provider.chat, and it landed at index 0
            # between the reset and this turn's classify — so the first cut of
            # this check measured the extraction prompt and went red. Find the
            # classifier by its own opening line instead of trusting ordering.
            classify_prompt = next(
                (p for p in provider.prompts if p.startswith("You route messages")), "",
            )
            check("open a folder" in classify_prompt.lower(),
                  "the live classifier prompt describes opening a folder",
                  f"{len(classify_prompt)} chars")

    finally:
        app.dependency_overrides.clear()
        server.should_exit = True
        await task
        print(f"\n  (scratch left at {scratch} — safe to delete)")

    passed = sum(1 for ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    for ok, label in RESULTS:
        if not ok:
            print(f"  FAILED: {label}")
    print("\n  NOT verifiable here (user-driven): whether the REAL model now "
          "labels 'open my downloads folder' as TASK. The catalog entry is in "
          "the prompt it reads; only a live run measures the verdict.")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
