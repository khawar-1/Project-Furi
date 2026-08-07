"""A/B: does naming "open a folder" in the classifier's INLINE rule move the
mode it picks?

The 2026-08-06 magic-words round MEASURED the analogous catalog edit at 12/12
in BOTH arms and recorded it as belt rather than cause. Whether a prompt line
is load-bearing is a measurement, never an inference from a previous round —
so this runs the same phrasings through the real model with the line and
without it, and the numbers decide whether the edit ships.

    venv\\Scripts\\python scripts\\_measure_open_mode.py
"""
import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.api import task_router  # noqa: E402
from app.providers.factory import create_provider  # noqa: E402

RUNS = 3

# Deliberately NOT the wording of any prompt example: a check whose input
# resembles a few-shot measures the prompt's memory (recorded 2026-08-02,
# after it happened twice).
CASES = [
    ("open donwloads", "TASK", "INLINE"),          # the incident, verbatim
    ("open folder 'fomi'", "TASK", "INLINE"),      # the incident, verbatim
    ("show me the reports folder on screen", "TASK", "INLINE"),
    ("pull up the folder where my invoices live", "TASK", "INLINE"),
    # Controls: these must NOT drift to INLINE.
    ("organize my downloads into folders by type", "TASK", "DELEGATE"),
    ("delete every tmp file on my desktop", "TASK", "DELEGATE"),
]


async def one(message: str) -> tuple[str, str]:
    # (provider, message, context) — checked against the real signature. The
    # first cut of this script had the argument order wrong, every call raised
    # identically in BOTH arms, and the run reported a confident "nothing
    # moved". A probe that cannot reach the code proves nothing, so this
    # asserts a plausible verdict before reporting one (see `main`).
    return await task_router._classify_message(create_provider(), message, "")


async def arm(label: str) -> dict:
    print(f"\n=== {label} ===")
    tally: dict[str, Counter] = {}
    for message, want_label, want_mode in CASES:
        results = await asyncio.gather(*(one(message) for _ in range(RUNS)))
        counts = Counter(f"{lab} {mode}" for lab, mode in results)
        hit = sum(n for k, n in counts.items() if k == f"{want_label} {want_mode}")
        tally[message] = counts
        print(f"  {message[:44]:<46} want {want_label} {want_mode:<8} "
              f"-> {hit}/{RUNS}  {dict(counts)}")
    return tally


def _positive_control_holds(tally: dict) -> bool:
    """The controls are unambiguous file WORK ("organize my downloads",
    "delete every tmp file"). If even those come back CHAT, the probe is not
    reaching the model and NO verdict from this run is evidence — the failure
    mode that produced a confident "nothing moved" from a run in which every
    single call raised."""
    controls = [m for m, lab, mode in CASES if mode == "DELEGATE"]
    return all(
        any(k.startswith("TASK") for k in tally[m]) for m in controls
    )


async def main() -> None:
    before = await arm("WITHOUT the line (current prompt)")
    if not _positive_control_holds(before):
        print("\n⚠️ POSITIVE CONTROL FAILED — unambiguous file work did not "
              "route TASK. The probe is broken; this run measures nothing.")
        return

    original = task_router._CLASSIFY_PROMPT
    if "open a folder" in original:
        print("\nthe line is already present - run this BEFORE editing the prompt")
        return
    patched = original.replace(
        '"what\'s on my desktop", "list my downloads"',
        '"what\'s on my desktop", "list my downloads", '
        '"open a folder on screen"',
    )
    assert patched != original, "anchor not found - update this script"
    task_router._CLASSIFY_PROMPT = patched
    try:
        after = await arm("WITH the line")
    finally:
        task_router._CLASSIFY_PROMPT = original

    print("\n=== VERDICT ===")
    moved = False
    for message, _, _ in CASES:
        if before[message] != after[message]:
            moved = True
            print(f"  MOVED: {message}\n     before {dict(before[message])}"
                  f"\n     after  {dict(after[message])}")
    if not moved:
        print("  no case moved - the edit is belt, not cause (do not claim it)")


if __name__ == "__main__":
    asyncio.run(main())
