"""A/B: does rewording plan RULE 20/21 stop the planner drafting `read_webpage`
for a bare "open <site>"? (2026-08-11)

WHY THIS EXISTS. The live incident — `open junaidjamshed.com` answered with the
whole homepage and no window ever opened — was fixed with a CODE comparator
(`planner._browse_substitution`). The rule wording was changed too, because rule
20 literally said "read_webpage is the DEFAULT way to open a URL" and the user
said "open". But this codebase has measured prompt-only fixes at ZERO three
separate times, and the 2026-08-06 round measured an analogous catalog edit at
12/12 in BOTH arms and honestly recorded it as belt rather than cause. Whether a
prompt line is load-bearing is a MEASUREMENT, never an inference from a previous
round. This produces the number.

WHAT IT MEASURES, AND WHY NOT THROUGH THE GUARD. It asks the real model for a
real plan draft and reads which tool step 0 uses. It deliberately does NOT go
through `_generate_steps`: that would apply the new guard and retry, so every
run would come back `browse` and the script would be measuring the comparator
while appearing to measure the prompt. The question here is only ever "what does
the model REACH FOR".

THE POSITIVE CONTROL IS LOAD-BEARING. A probe that cannot reach the code prints
"nothing moved", which looks exactly like a verdict — recorded twice
(2026-08-06 `_measure_open_mode`, 2026-08-09 `_verify_store_search`). So a
content-read goal must draft `read_webpage` in BOTH arms; if it does not, the
probe refuses to report.

Run from backend/:

    venv\\Scripts\\python -u scripts\\_measure_open_url_routing.py

NEVER collected by pytest (real API calls, real credits).
"""
from __future__ import annotations

import asyncio
import functools
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

print = functools.partial(print, flush=True)  # noqa: A001

import app.tools  # noqa: E402,F401 — registers the real tools, so the catalog is real
from app.agents import planner as P  # noqa: E402
from app.agents.agent_registry import agent_for_label  # noqa: E402
from app.providers.base import LLMMessage  # noqa: E402
from app.providers.factory import create_provider  # noqa: E402

RUNS = 3
BROWSER = agent_for_label("BROWSE")

# The exact sentences this round replaced. The "before" arm restores them in the
# built prompt, so the two arms differ by these bytes and nothing else.
NEW_20 = ("20. read_webpage is the DEFAULT way to READ THE CONTENT of a URL — an "
          "article, a docs page, a listing you need the text of:")
OLD_20 = "20. read_webpage is the DEFAULT way to open a URL:"

NEW_20_TAIL = (
    " It FETCHES text and returns it; it never puts a browser window on the "
    "user's screen and the user never sees the page, so it is NOT how you "
    "\"open\" or \"go to\" a site for someone (that is browse, rule 21) — using "
    "it there answers with a wall of page text while nothing actually opens."
)
NEW_21_TAIL = (
    " Putting a site ON SCREEN is browse too: when the whole request is to open "
    "or go to a site (\"open junaidjamshed.com\", \"go to youtube\", \"pull up "
    "amazon\") with nothing to look up or fetch from it, that is ONE browse step "
    "with start_url set to that site — it opens a real browser window and leaves "
    "it open, which is what the user asked for. Never answer that request with "
    "read_webpage / browse_page / web_search."
)

# ⚠️ PARAPHRASES, NOT THE RULE'S OWN WORDS. A check whose input resembles a
# prompt example measures the prompt's memory — recorded 2026-08-02 after it
# happened twice in one day. Only the first is the incident verbatim; the rule
# text names it, so it is reported separately from the paraphrases below.
INCIDENT = ("open junaidjamshed.com", "browse")

CASES = [
    # Bare navigation: the user wants a window. None of these appear in the rules.
    ("open flipkart.com", "browse"),
    ("go to bbc.co.uk", "browse"),
    ("pull up my bank's website hbl.com", "browse"),
    ("open daraz.pk on screen", "browse"),
    # CONTROLS — genuine content reads. These must stay read_webpage in BOTH
    # arms, or the reword has broken the tool it was clarifying.
    ("what does https://example.com/pricing say about their enterprise tier",
     "read_webpage"),
    ("summarise the article at https://example.com/blog/post-1", "read_webpage"),
]

# The control that proves the probe reaches the model at all.
POSITIVE_CONTROL = CASES[-1]

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _prompt(goal: str, *, old_rules: bool) -> str:
    """The REAL plan prompt, optionally with this round's wording reverted."""
    text = P._build_plan_prompt(
        goal, "", "", "", "", tools=BROWSER.tools, persona=BROWSER.persona,
    )
    if old_rules:
        text = text.replace(NEW_20, OLD_20)
        text = text.replace(NEW_20_TAIL, "")
        text = text.replace(NEW_21_TAIL, "")
    return text


def _first_tool(raw: str) -> str:
    m = _JSON_RE.search(raw or "")
    if not m:
        return "<unparseable>"
    try:
        steps = json.loads(m.group(0)).get("steps") or []
    except Exception:
        return "<unparseable>"
    return str(steps[0].get("tool")) if steps else "<no-steps>"


async def _one(goal: str, *, old_rules: bool) -> str:
    provider = create_provider()
    resp = await provider.chat(
        [LLMMessage(role="user", content=_prompt(goal, old_rules=old_rules))],
        temperature=0,
        max_tokens=8192,
    )
    return _first_tool(resp.content)


async def _arm(label: str, *, old_rules: bool) -> dict[str, Counter]:
    print(f"\n=== {label} ===")
    out: dict[str, Counter] = {}
    for goal, want in [INCIDENT, *CASES]:
        tools = await asyncio.gather(
            *(_one(goal, old_rules=old_rules) for _ in range(RUNS))
        )
        counts = Counter(tools)
        hit = counts[want]
        flag = "ok " if hit == RUNS else ("MISS" if hit == 0 else "part")
        print(f"  [{flag}] {hit}/{RUNS} {want:<13} {goal[:58]:<58} {dict(counts)}")
        out[goal] = counts
    return out


async def main() -> int:
    before = await _arm("BEFORE — rule 20 says 'the DEFAULT way to open a URL'",
                        old_rules=True)
    after = await _arm("AFTER — rule 20 reads content, rule 21 owns 'open <site>'",
                       old_rules=False)

    ctrl_goal, ctrl_want = POSITIVE_CONTROL
    if before[ctrl_goal][ctrl_want] == 0 or after[ctrl_goal][ctrl_want] == 0:
        print("\n!! POSITIVE CONTROL FAILED — a genuine content read did not draft "
              "read_webpage in one or both arms. The probe is not measuring what "
              "it claims; NO VERDICT.")
        return 2

    print("\n=== VERDICT ===")
    moved = 0
    for goal, want in [INCIDENT, *CASES]:
        b, a = before[goal][want], after[goal][want]
        if b != a:
            moved += 1
        arrow = "->" if b != a else "=="
        print(f"  {b}/{RUNS} {arrow} {a}/{RUNS}  {want:<13} {goal[:58]}")
    total_b = sum(before[g][w] for g, w in [INCIDENT, *CASES])
    total_a = sum(after[g][w] for g, w in [INCIDENT, *CASES])
    n = (len(CASES) + 1) * RUNS
    print(f"\n  correct: {total_b}/{n} before -> {total_a}/{n} after "
          f"({moved} case(s) moved)")
    print("  " + (
        "The reword is CAUSE — it changes what the model reaches for."
        if total_a > total_b else
        "The reword is BELT — the code comparator is what fixes this. "
        "Recorded as measured, not as claimed."
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
