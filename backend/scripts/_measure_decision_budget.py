"""
Furi OS — what does a COMMIT decision really cost? (2026-08-10)

The incident's own log shows the decision call returning an EMPTY string three
times on a product page, at `decide_ms` 24-30s:

    00:36:36  browse: decision reply did not parse into an action - retrying once.
              Reply was: ''
    00:36:57  browse: decision retry also produced no action - stopping

That is the recorded reasoning-model landmine: on a thinking model the reasoning
tokens come out of `max_tokens`, so a cap that is too small returns NOTHING —
and here that reads as "no usable action" and kills the browse. `_DECISION_MAX_TOKENS`
is 2048, and the retry repeats the SAME call at the SAME cap.

This measures it instead of guessing (the season.py discipline, where 512, 1024,
2048 and 4096 all returned empty and shortening the prompt made it WORSE): it
builds the REAL commit decision prompt from a REAL observation of the incident's
page and calls the REAL model at a ladder of caps, printing what came back.

    venv\\Scripts\\python -u scripts\\_measure_decision_budget.py [url]

NEVER collected by pytest (real browser, real network, real credits).
"""
from __future__ import annotations

import asyncio
import functools
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

print = functools.partial(print, flush=True)  # noqa: A001

DEFAULT_URL = (
    "https://www.junaidjamshed.com/products/"
    "black-blended-kameez-shalwar-jjksa30729r52ap"
)
GOAL = (
    "Go to junaidjamshed.com, search for a black kameez kurta, open it, and add "
    "it to the cart."
)
CAPS = (2048, 4096, 8192, 12288)


async def _main_async(url: str) -> int:
    from app.browser import loop as browser_loop
    from app.browser import observe as dom_observe
    from app.browser import session as browser_session
    from app.browser.runtime import run_browser
    from app.browser.session import ensure_playwright_driver
    from app.providers.base import LLMMessage
    from app.providers.factory import build_provider

    await run_browser(ensure_playwright_driver(), timeout=90)

    async def _work() -> int:
        host = (urlparse(url).hostname or "").lower()
        session = await browser_session.BrowserSession.open({host})
        provider = build_provider()
        try:
            await session.goto(url)
            await session.settle()
            obs = await dom_observe.observe(session.page)
            prompt = browser_loop._DECISION_PROMPT.format(
                goal=GOAL,
                page=dom_observe.render(obs, skip_elements=0),
                relevant="",
                memory="",
                history="\n",
                extract_action="",
                extract_rule="",
                drag_action="",
                drag_rule="",
                more_action="",
                profile="",
                allowed=host,
                commit_action=browser_loop._COMMIT_ACTION_LINE,
                upload_action="",
                commit_rules=browser_loop._COMMIT_RULES,
                upload_rules="",
                vision_note="",
                read_rule="",
            )
            print(f"page      : {obs.element_total} elements, "
                  f"{len(obs.page_text or '')} chars of text")
            print(f"PROMPT    : {len(prompt)} chars (~{len(prompt) // 4} tokens)")
            print(f"current _DECISION_MAX_TOKENS = {browser_loop._DECISION_MAX_TOKENS}\n")

            for cap in CAPS:
                try:
                    reply = await asyncio.wait_for(
                        provider.chat(
                            messages=[LLMMessage(role="user", content=prompt)],
                            temperature=0.0,
                            max_tokens=cap,
                        ),
                        timeout=120,
                    )
                    content = (reply.content or "")
                    used = getattr(reply, "tokens_used", None)
                    parsed = browser_loop._parse_action(content)
                    print(f"  cap {cap:>6}: used={used} len={len(content)} "
                          f"parsed={parsed} reply={content[:120]!r}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  cap {cap:>6}: FAILED {type(exc).__name__}: {exc}")
        finally:
            try:
                await provider.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            await session.close()
        return 0

    return await run_browser(_work(), timeout=600)


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    code = 1
    try:
        code = loop.run_until_complete(_main_async(url))
    finally:
        try:
            from app.browser.runtime import shutdown_browser_runtime

            shutdown_browser_runtime()
        except Exception:  # noqa: BLE001
            pass
    sys.stdout.flush()
    import os

    os._exit(code)


if __name__ == "__main__":
    raise SystemExit(main())
