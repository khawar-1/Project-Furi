"""
Jarvis OS — what does the "add janan to cart" page ACTUALLY look like? (2026-08-08)

WHY THIS EXISTS. The live run stopped with "couldn't work out a safe next action
on this page" after `_decide` returned an EMPTY string twice on
`junaidjamshed.com/search?q=janan`. Two explanations fit that symptom and they
imply DIFFERENT fixes:

    (a) the decision prompt overflowed the reasoning budget — the recorded
        _DECISION_MAX_TOKENS landmine, fixed by a bigger cap;
    (b) the model was asked to pick between things the user's own words cannot
        tell apart, i.e. to GUESS — fixed by asking the user first.

Only the page can say. This measures both, plus the numbers the new pre-decision
ask has to be bounded by: how many things tie on the HOMEPAGE (where asking would
be premature — the model correctly searched first) versus on the RESULTS page.

Run from backend/:

    venv\\Scripts\\python -u scripts\\_measure_janan_choice.py

NEVER collected by pytest (drives a real browser over the real network).
"""
from __future__ import annotations

import asyncio
import functools
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

print = functools.partial(print, flush=True)  # noqa: A001

RESULTS_DIR = Path(__file__).resolve().parent / "bench-results"

# The incident, verbatim from the trace: the goal is the PLANNER's paraphrase
# (note it keeps the user's typo'd host, which is why that token matches nothing)
# and `intent` is what the user actually typed.
GOAL = "Go to junadjamshed.com, find the 'janan' product, and add it to the cart."
INTENT = "go to junadjamshed.com and add janan in cart"

PAGES = [
    "https://www.junaidjamshed.com/",
    "https://www.junaidjamshed.com/search?q=janan",
    # A second listing, because the user's point is that the axes VARY: a perfume
    # has a size, a trouser has a waist as well, and nothing may hardcode either.
    "https://www.junaidjamshed.com/search?q=trouser",
]

# After each listing, follow the FIRST tied candidate into its detail page — the
# page where the size/quantity question has to be asked, and whose shape must be
# measured rather than imagined (the recorded rule: a fake page is a claim about
# the live DOM — check it).
FOLLOW_FIRST = True

# The three phrasings the user described, to check the tie rule answers each
# correctly on the REAL listing rather than on an invented one.
PHRASINGS = [
    "add janan to cart",
    "add janan sports to cart",
    "add janan sports 100ml to cart",
]


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


async def _look(session, url: str) -> dict:
    from app.browser import choice
    from app.browser import extract as browser_extract
    from app.browser import loop as browser_loop
    from app.browser import observe as dom_observe

    await session.goto(url)
    await session.settle()
    obs = await dom_observe.observe(session.page)

    row: dict = {
        "url": obs.url,
        "title": obs.title,
        "elements": obs.element_total,
        "text_len": len(obs.page_text or ""),
    }

    cands = choice.candidates_of(obs.elements, find_price=browser_extract.find_price)
    row["candidates"] = len(cands)
    row["with_price"] = sum(1 for c in cands if c.price)

    # The size of what the decision prompt is MOSTLY made of — hypothesis (a).
    # The prompt builder is a closure inside `_decide`, so the render is the
    # honest proxy: it is the observation half the page controls, and the only
    # part that varies with the page.
    row["rendered_chars"] = len(dom_observe.render(obs))
    row["decision_cap"] = browser_loop._DECISION_MAX_TOKENS

    per_phrasing = {}
    for phrase in PHRASINGS:
        target = choice.target_tokens(phrase, obs.url)
        tied = choice.tied_candidates(
            target, obs.elements, find_price=browser_extract.find_price, limit=99
        )
        # How many candidates score at all, so "tied" can be read against the pool
        scored = [
            (choice._score(target, c.label), c) for c in cands
        ]
        scored = [(s, c) for s, c in scored if s > 0]
        per_phrasing[phrase] = {
            "target": choice.plain_words(target),
            "matched": len(scored),
            "tied": len(tied),
            "top_score": max([s for s, _ in scored], default=0),
            "options": [c.option() for c in tied[:40]],
            "hrefs": [c.href for c in tied[:3]],
            "page_is_the_target": choice.page_is_the_target(
                target, obs.title, obs.url, tied
            ),
        }
    row["phrasings"] = per_phrasing

    # And the goal exactly as the loop reads it today (`_target_words` uses the
    # planner's GOAL, not the user's words — recorded here because that is a
    # difference the fix may have to close).
    goal_target = choice.target_tokens(GOAL, obs.url)
    intent_target = choice.target_tokens(INTENT, obs.url)
    row["goal_target"] = choice.plain_words(goal_target)
    row["intent_target"] = choice.plain_words(intent_target)
    row["goal_tied"] = len(
        choice.tied_candidates(
            goal_target, obs.elements, find_price=browser_extract.find_price, limit=99
        )
    )

    # EVERY element, by role, so the variant gate is designed against the DOM the
    # site really serves rather than an imagined one (the recorded rule: a fake
    # page is a claim about the live DOM — check it).
    by_role: dict[str, int] = {}
    for e in obs.elements:
        by_role[str(e.role or "?")] = by_role.get(str(e.role or "?"), 0) + 1
    row["by_role"] = by_role
    controls = [
        {
            "role": str(e.role or ""),
            "name": (getattr(e, "name_full", "") or e.name or "")[:70],
            "index": e.index,
            "value": str(getattr(e, "value", "") or "")[:40],
            "form": bool(getattr(e, "form", False)),
            "in_dialog": bool(getattr(e, "in_dialog", False)),
        }
        for e in obs.elements
        if str(e.role or "").lower() not in {"link", "heading", "listitem", "article"}
    ]
    row["controls"] = controls[:40]
    row["control_count"] = len(controls)
    return row


async def _main_async() -> int:
    from app.browser import session as browser_session
    from app.browser.runtime import run_browser
    from app.browser.session import ensure_playwright_driver

    try:
        await run_browser(ensure_playwright_driver(), timeout=90)
        print("playwright driver warm")
    except Exception as exc:  # noqa: BLE001
        print(f"driver warm-up skipped: {type(exc).__name__}: {exc}")

    async def _work() -> list[dict]:
        from urllib.parse import urljoin

        session = await browser_session.BrowserSession.open({_host(PAGES[0])})
        rows: list[dict] = []
        try:
            queue = list(PAGES)
            while queue:
                url = queue.pop(0)
                print(f"\n=== {url}")
                row = await _look(session, url)
                rows.append(row)
                if FOLLOW_FIRST and "/search" in url:
                    first = next(
                        (
                            o
                            for d in row["phrasings"].values()
                            for o in d.get("hrefs", [])
                            if o
                        ),
                        "",
                    )
                    if first:
                        queue.append(urljoin(row["url"], first))
                print(
                    f"    {row['elements']} elements, {row['candidates']} candidates "
                    f"({row['with_price']} priced), rendered {row['rendered_chars']} chars, "
                    f"cap {row['decision_cap']} tokens, {row['control_count']} controls"
                )
                print(f"    goal words   {row['goal_target']} -> {row['goal_tied']} tie")
                print(f"    intent words {row['intent_target']}")
                for phrase, d in row["phrasings"].items():
                    print(
                        f"    {phrase!r}\n"
                        f"        said={d['target']} matched={d['matched']} "
                        f"tied={d['tied']} top={d['top_score']} "
                        f"page_is_target={d['page_is_the_target']}"
                    )
                    for opt in d["options"]:
                        print(f"          - {opt}")
                print(f"    roles: {row['by_role']}")
                for c in row["controls"]:
                    print(
                        f"      [{c['index']:>3}] {c['role']:<10} form={int(c['form'])} "
                        f"value={c['value']!r} {c['name']!r}"
                    )
        finally:
            await session.close()
        return rows

    rows = await run_browser(_work(), timeout=600)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / "janan-choice.json"
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


def main() -> int:
    # THE SELECTOR LOOP production actually has — the browse_observe_profile.py
    # rule: a standalone asyncio.run() is Proactor on Windows, which hides the
    # whole Playwright-subprocess bug class.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    code = 1
    try:
        print(f"event loop: {type(loop).__name__}")
        code = loop.run_until_complete(_main_async())
    finally:
        try:
            from app.browser.runtime import shutdown_browser_runtime

            shutdown_browser_runtime()
        except Exception:  # noqa: BLE001
            pass
    sys.stdout.flush()
    sys.stderr.flush()
    import os

    os._exit(code)


if __name__ == "__main__":
    raise SystemExit(main())
