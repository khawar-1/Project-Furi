"""A/B the classifier catalog edit against the REAL model (2026-08-06).

The runtime probe proves the phrase is in the prompt the model reads. It cannot
prove the model ROUTES on it — that is a measurement, and the 2026-08-03 round
recorded exactly this distinction when conversation search went 0/3 to 3/3 on a
catalog edit alone.

Two arms over the SAME phrasings, same temperature, in one process:
  BEFORE  the catalog line describing open_folder is stripped back out
  AFTER   the shipped prompt

Small and honest about cost: len(PHRASINGS) x 2 x REPEATS tiny temp-0 calls.
This provider's temp-0 is NOT deterministic (recorded), hence REPEATS.

Run:  venv\\Scripts\\python scripts\\_measure_folder_routing.py
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REPEATS = 3

# The incident's own wording first, then ordinary ways of asking for the same
# thing. NOT the phrasing used in the catalog itself — the 2026-08-02 rule: a
# check whose input resembles a prompt example measures the prompt's memory.
PHRASINGS = [
    ("furi open fomi folder", {"TASK", "DESKTOP"}),
    ("open my downloads folder", {"TASK", "DESKTOP"}),
    ("show me where that report lives", {"TASK", "DESKTOP"}),
    ("pull up the phase3test folder on screen", {"TASK", "DESKTOP"}),
]

# Controls: these must NOT drift to a file/desktop label because of the edit.
CONTROLS = [
    ("what is the capital of france", {"WEB", "CHAT"}),
    ("i finally cleaned up my downloads folder", {"CHAT"}),
]

CATALOG_LINE = (
    "OPEN A FOLDER in a file-explorer window on screen "
    "(or show the user where a file lives), "
)


async def arm(label: str, strip: bool) -> dict:
    from app.api import task_router as tr
    from app.providers.factory import build_provider

    original = tr._CLASSIFY_PROMPT
    if strip:
        assert CATALOG_LINE in original, "catalog line not found — script is stale"
        tr._CLASSIFY_PROMPT = original.replace(CATALOG_LINE, "")

    provider = build_provider()
    results: dict[str, Counter] = {}
    try:
        for text, _ok in PHRASINGS + CONTROLS:
            counter: Counter = Counter()
            for _ in range(REPEATS):
                verdict, _mode = await tr._classify_message(provider, text, "")
                counter[verdict] += 1
            results[text] = counter
    finally:
        tr._CLASSIFY_PROMPT = original
        close = getattr(provider, "__aexit__", None)
        if close:
            try:
                await close(None, None, None)
            except Exception:
                pass
    return results


def score(results: dict) -> tuple[int, int]:
    hit = total = 0
    for text, ok in PHRASINGS:
        total += REPEATS
        hit += sum(n for label, n in results[text].items() if label in ok)
    return hit, total


async def main() -> int:
    print(f"A/B over {len(PHRASINGS)} folder phrasings x {REPEATS} runs, real model\n")

    before = await arm("BEFORE", strip=True)
    after = await arm("AFTER", strip=False)

    print(f"  {'phrasing':44} {'BEFORE':22} AFTER")
    for text, ok in PHRASINGS:
        b = ", ".join(f"{k}x{v}" for k, v in before[text].most_common())
        a = ", ".join(f"{k}x{v}" for k, v in after[text].most_common())
        mark = "  <- wanted " + "/".join(sorted(ok))
        print(f"  {text[:44]:44} {b:22} {a}{mark}")

    print()
    for text, ok in CONTROLS:
        b = ", ".join(f"{k}x{v}" for k, v in before[text].most_common())
        a = ", ".join(f"{k}x{v}" for k, v in after[text].most_common())
        drift = "" if all(k in ok for k in after[text]) else "   <- ⚠️ DRIFTED"
        print(f"  CONTROL {text[:36]:36} {b:22} {a}{drift}")

    bh, bt = score(before)
    ah, at = score(after)
    print(f"\n  ROUTED CORRECTLY:  before {bh}/{bt}   after {ah}/{at}")
    if ah > bh:
        print("  => the catalog entry MOVED the real model's verdict")
    elif ah == bh == at:
        print("  => already perfect in both arms; the edit is belt, not the lever")
    else:
        print("  => NO measurable improvement — do not claim one")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
