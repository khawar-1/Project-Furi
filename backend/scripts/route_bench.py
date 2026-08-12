"""Furi OS - routing acceptance benchmark.

WHY THIS EXISTS. `browse_bench.py` is a scored gate with real numbers; routing
had none. And the asymmetry inside routing was worse than "untested": the
deterministic half is heavily covered (`test_task_router.py`, 78KB of gate
assertions) while the LLM half - the part that decides what actually happens -
was covered nowhere. That asymmetry IS the 2026-07-17 finding: MEASURED, the
gate blocked 7 of 8 external-fact questions while the classifier labelled 8/8
correctly. Nobody could see that, because nobody was counting either half.

So this counts both, separately, and never mixes them:

  GATE RECALL     of the cases that SHOULD route, how many passed the
                  deterministic gate. A miss here cannot be fixed downstream -
                  the classifier is never asked. This is the number that was 1/8.
  GATE COST       of the cases that should stay CHAT, how many fired the gate
                  anyway. NOT a failure - over-inclusiveness is deliberate
                  ("tuned for RECALL… the LLM confirmation prunes it") - but it
                  is the price of that choice, and it had never been measured.
  LABEL ACCURACY  of the cases that reached the classifier, how many got the
                  right label.
  END-TO-END      what the user would actually have experienced.

It calls `task_router.decide_route` - the SAME function the chat turn calls, not
a copy of its logic. That is deliberate: this project has shipped "the test drove
a shape the product doesn't use" four times (2026-07-17 fan-out tests that
bypassed the planner, 2026-07-30 bulk tests that drove the singular tool,
2026-08-01 a page fake returning one object twice, 2026-08-02 grid fakes with no
quick-add button). A bench scoring a re-implementation of routing would be the
fifth.

⚠️ --repeat IS NOT OPTIONAL POLISH. DeepSeek's temperature-0 is not
deterministic - CLAUDE.md records this exact message set returning
"CHAT, BROWSE, CHAT" on three runs of ONE input. A single run of this bench
measures one sample of a distribution. Wrong-every-time and right-3-times-in-5
are different defects needing different fixes, so they are reported separately.

Run from backend/:

    venv\\Scripts\\python scripts\\route_bench.py                 # one pass
    venv\\Scripts\\python scripts\\route_bench.py --repeat 3      # + stability
    venv\\Scripts\\python scripts\\route_bench.py --gate-only     # zero credits
    venv\\Scripts\\python scripts\\route_bench.py --only fifa-qualified-2026
    venv\\Scripts\\python scripts\\route_bench.py --list

EXIT CODE: 1 when a case is wrong in EVERY run (a hard, reproducible failure);
0 when the only failures are flaky. The distinction is the point - a permanently
red gate on a known-variable classifier would just get ignored.

NEVER collected by pytest (lives outside tests/ and spends real provider
credits). The hermetic suite proves the gate; this proves the whole decision.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# These reports print prose straight out of the case files, which carry
# em-dashes and arrows. On a legacy cp1252 console that renders as mojibake -
# and the known_gap block is the line you most need to read. Ask for UTF-8 and
# fall back to a plain "?" rather than garbage; the scripts' OWN literals are
# kept ASCII so the fixed text always reads correctly either way.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):  # pragma: no cover - non-reconfigurable stream
    pass

print = functools.partial(print, flush=True)  # noqa: A001

CASES_FILE = Path(__file__).resolve().parent / "route_cases.json"
RESULTS_DIR = Path(__file__).resolve().parent / "bench-results"


class _GateOnlyProvider:
    """Stands in for the real model when --gate-only is set. The gate still runs
    inside the real decide_route, so the gate numbers are exactly as true as in a
    full run; the label numbers are simply not scored."""

    model_name = "gate-only-stub"
    provider_name = "stub"

    class _Reply:
        content = "CHAT"

    async def chat(self, messages, **kwargs):
        return self._Reply()


def _expected_labels(case: dict) -> list[str]:
    expect = case.get("expect") or {}
    if expect.get("label_any"):
        return [str(x).upper() for x in expect["label_any"]]
    label = expect.get("label")
    return [str(label).upper()] if label else []


def _build_conversation(case: dict) -> str:
    """Render the case's prior turns EXACTLY as production does, by handing them
    to the real `conversation_context` rather than formatting them here - the
    same no-re-implementation rule the module docstring argues for."""
    from app.api.task_router import conversation_context
    from app.db.schemas import ChatMessage, ChatRequest

    turns = case.get("conversation") or []
    if not turns:
        return ""
    messages = [ChatMessage(role=t["role"], content=t["content"]) for t in turns]
    messages.append(ChatMessage(role="user", content=case["message"]))
    return conversation_context(ChatRequest(messages=messages))


async def _run_once(case: dict, provider, gate_only: bool) -> dict:
    from app.api import task_router
    from app.core import routing_trace

    conversation = _build_conversation(case)

    # Some cases only reach the classifier because a browser tab is open
    # (is_browse_followup reads the live tab registry). Simulate it rather than
    # launching Chromium.
    patched = None
    if case.get("browse_window"):
        patched = task_router._browse_window_active
        task_router._browse_window_active = lambda: True
    try:
        routing_trace.reset()
        started = time.perf_counter()
        decision = await task_router.decide_route(
            case["message"], conversation, provider
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
    finally:
        if patched is not None:
            task_router._browse_window_active = patched
        routing_trace.reset()

    wanted = _expected_labels(case)
    should_route = bool(wanted) and wanted != ["CHAT"]
    got = decision.label if decision.routed else "CHAT"

    label_ok = None if gate_only else (got in wanted if wanted else None)
    mode_ok = None
    want_mode = (case.get("expect") or {}).get("mode")
    if want_mode and not gate_only:
        mode_ok = decision.trace.execution == (
            "delegate" if str(want_mode).upper() == "DELEGATE" else "inline"
        )

    return {
        "gate_fired": bool(decision.trace.gate_fired),
        "gate_reason": decision.trace.gate_reason,
        "should_route": should_route,
        "got": got,
        "wanted": wanted,
        "label_ok": label_ok,
        "mode_ok": mode_ok,
        "execution": decision.trace.execution,
        "fail_open_reason": decision.fail_open_reason,
        "classifier_ms": decision.trace.classifier_ms,
        "classifier_error": decision.trace.classifier_error,
        "bare_navigation": decision.trace.bare_navigation,
        "ms": elapsed_ms,
        # END-TO-END is what the user would have experienced: an action case had
        # to route AND carry the right label; a CHAT case had to fall open.
        #
        # In --gate-only the classifier is a stub, so a label verdict would be
        # meaningless - score the ONE thing that mode can actually see: did every
        # case that should route reach the classifier at all? A CHAT case firing
        # the gate is not a failure there (it is the deliberate recall-first
        # cost, counted separately as GATE COST), so it always passes.
        "end_to_end_ok": (
            (bool(decision.trace.gate_fired) if should_route else True)
            if gate_only else
            ((decision.routed and got in wanted) if should_route
             else (not decision.routed))
        ),
    }


async def _main_async(args) -> int:
    from app.providers.factory import create_provider

    payload = json.loads(CASES_FILE.read_text("utf-8"))
    cases = [c for c in payload["cases"] if not str(c.get("id", "")).startswith("_")]
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]
        if not cases:
            print(f"no case matched {args.only}")
            return 2

    if args.gate_only:
        provider = _GateOnlyProvider()
        print("gate-only: the classifier is stubbed - label numbers are NOT scored")
    else:
        provider = create_provider()
        print(f"provider: {provider.provider_name} / {provider.model_name}")
    print(f"cases: {len(cases)}   repeat: {args.repeat}\n")

    rows = []
    for case in cases:
        runs = []
        for _ in range(args.repeat):
            try:
                runs.append(await _run_once(case, provider, args.gate_only))
            except Exception as exc:  # noqa: BLE001 - one bad case must not end the run
                runs.append({"error": f"{type(exc).__name__}: {exc}",
                             "end_to_end_ok": False, "gate_fired": False,
                             "should_route": True, "got": "?", "wanted": [],
                             "label_ok": False, "mode_ok": None, "ms": 0})
        ok = sum(1 for r in runs if r["end_to_end_ok"])
        # A KNOWN gap is a limitation we have measured and chosen to live with
        # (or have not fixed yet). It is still scored and still shown - so a fix
        # or a regression is visible - but it does not turn the gate red, because
        # a gate that is permanently red stops being read. That is the same
        # failure as a no-op that reports success, in the other direction.
        known = bool(case.get("known_gap"))
        verdict = (
            "PASS " if ok == len(runs)
            else ("KNOWN" if known else ("FLAKY" if ok else "FAIL "))
        )
        first = runs[0]
        detail = f"{first['got']}"
        if first.get("execution"):
            detail += f"/{first['execution']}"
        if not first["gate_fired"]:
            detail += "  [gate closed]"
        elif first.get("gate_reason"):
            detail += f"  [{first['gate_reason']}]"
        if first.get("classifier_error"):
            detail += f"  !{first['classifier_error'][:40]}"
        want = "|".join(first["wanted"]) or "?"
        print(f"  {verdict} {case['id']:28} want {want:16} got {detail}"
              + (f"   ({ok}/{len(runs)})" if args.repeat > 1 else ""))
        rows.append({"id": case["id"], "why": case.get("why", ""),
                     "known_gap": case.get("known_gap", ""),
                     "ok_runs": ok, "runs": runs, "verdict": verdict.strip()})

    _report(rows, args)
    _write(rows, args)
    # Wrong in EVERY run = a real, reproducible failure. Flaky = variance, which
    # is a finding rather than a break - see the module docstring. A known gap
    # never fails the gate; it is listed on every run instead.
    return 1 if any(
        r["ok_runs"] == 0 and not r["known_gap"] for r in rows
    ) else 0


def _report(rows: list[dict], args) -> None:
    first_runs = [r["runs"][0] for r in rows]

    action = [r for r in first_runs if r["should_route"]]
    chat = [r for r in first_runs if not r["should_route"]]
    gate_hits = sum(1 for r in action if r["gate_fired"])
    gate_cost = sum(1 for r in chat if r["gate_fired"])

    print("\n" + "=" * 72)
    print(f"GATE RECALL     {gate_hits}/{len(action)} action cases passed the gate")
    if gate_hits < len(action):
        # ASCII only in printed output: a Windows console is cp1252 and turns
        # emoji into mojibake, which makes the one line you most need to read
        # the least readable one on screen.
        print("                !! a gate miss CANNOT be fixed downstream - the")
        print("                   classifier is never asked. Missed:")
        for run, case in zip(first_runs, rows):
            if run["should_route"] and not run["gate_fired"]:
                print(f"                    {case['id']}")
    print(f"GATE COST       {gate_cost}/{len(chat)} chat cases fired the gate")
    print("                (one wasted temp-0 call each - the deliberate price")
    print("                 of a recall-first gate, not a failure)")

    if not args.gate_only:
        scored = [r for r in first_runs if r["label_ok"] is not None]
        label_ok = sum(1 for r in scored if r["label_ok"])
        print(f"LABEL ACCURACY  {label_ok}/{len(scored)} classified cases got the right label")
        modes = [r for r in first_runs if r["mode_ok"] is not None]
        if modes:
            print(f"MODE            {sum(1 for r in modes if r['mode_ok'])}/{len(modes)} "
                  "cases got the expected inline/delegate")

    e2e = sum(1 for r in first_runs if r["end_to_end_ok"])
    if args.gate_only:
        print(f"SCORED          {e2e}/{len(first_runs)} (gate-only: action cases that "
              "reached the classifier)")
    else:
        print(f"END-TO-END      {e2e}/{len(first_runs)} cases routed as the user would expect")

    if args.repeat > 1:
        solid = sum(1 for r in rows if r["ok_runs"] == args.repeat)
        flaky = sum(1 for r in rows if 0 < r["ok_runs"] < args.repeat)
        broken = sum(1 for r in rows if r["ok_runs"] == 0)
        print(f"\nSTABILITY over {args.repeat} runs: "
              f"{solid} always right, {flaky} FLAKY, {broken} always wrong")
        if flaky:
            print("  A flaky case is variance in the model, not a bug in the gate.")
            print("  Do not 'fix' it with a prompt edit - that has measured ZERO")
            print("  three times in this codebase. Measure it.")
            for r in rows:
                if 0 < r["ok_runs"] < args.repeat:
                    got = Counter(run["got"] for run in r["runs"])
                    print(f"    {r['id']:28} {r['ok_runs']}/{args.repeat}  "
                          f"{', '.join(f'{k}×{v}' for k, v in got.most_common())}")

    if not args.gate_only:
        latency = [
            run["classifier_ms"] for r in rows for run in r["runs"]
            if run.get("classifier_ms")
        ]
        if latency:
            ordered = sorted(latency)
            print(f"\nclassifier: {len(latency)} calls, "
                  f"p50 {ordered[len(ordered) // 2]}ms, max {ordered[-1]}ms")

    gaps = [r for r in rows if r["known_gap"] and r["ok_runs"] < args.repeat]
    if gaps:
        print(f"\n--- KNOWN GAPS ({len(gaps)}) - measured, not fixed, not counted "
              + "-" * 11)
        for r in gaps:
            first = r["runs"][0]
            print(f"  {r['id']}: wanted {'|'.join(first['wanted'])}, got {first['got']}")
            for line in _wrap(r["known_gap"], 88):
                print(f"      {line}")
    healed = [r for r in rows if r["known_gap"] and r["ok_runs"] == args.repeat]
    if healed:
        print(f"\n>> {len(healed)} known gap(s) now PASS - retire the known_gap flag:")
        for r in healed:
            print(f"     {r['id']}")

    wrong = [r for r in rows if r["ok_runs"] < args.repeat and not r["known_gap"]]
    if wrong:
        print("\n--- REGRESSIONS: cases that did not always route as expected "
              + "-" * 11)
        for r in wrong:
            first = r["runs"][0]
            print(f"  {r['id']}: wanted {'|'.join(first['wanted'])}, got {first['got']}"
                  f" ({r['ok_runs']}/{args.repeat})")
            if r["why"]:
                for line in _wrap(r["why"], 88):
                    print(f"      {line}")


def _wrap(text: str, width: int) -> list[str]:
    import textwrap
    return textwrap.wrap(text, width) or [""]


def _write(rows: list[dict], args) -> None:
    try:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        path = RESULTS_DIR / f"route-{stamp}.json"
        path.write_text(json.dumps({
            "when": stamp,
            "repeat": args.repeat,
            "gate_only": args.gate_only,
            "cases": rows,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nwritten: {path}")
    except Exception as exc:  # noqa: BLE001
        print(f"could not write results: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repeat", type=int, default=1,
                        help="runs per case; >1 separates flaky from broken")
    parser.add_argument("--gate-only", action="store_true",
                        help="stub the classifier - deterministic, zero credits")
    parser.add_argument("--only", nargs="*", default=None, metavar="CASE_ID")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if args.list:
        for case in json.loads(CASES_FILE.read_text("utf-8"))["cases"]:
            want = "|".join(_expected_labels(case)) or "?"
            print(f"{case['id']:30} {want:16} {case['message'][:60]}")
        return 0

    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
