"""Jarvis OS - routing audit report.

Reads the `routing_decisions` table and answers the questions you actually have
after something didn't happen:

  * where did turns go, and how often did they fall open to chat?
  * WHY did they fall open - the gate never fired, the model judged it
    conversation, or the model call failed? Three causes, three different fixes,
    and they look identical from the user's seat.
  * which turns are CONFIRMED misses - ones where the chat path itself said so?
  * what does the classifier cost, and which model produced these verdicts?

Run from backend/:

    venv\\Scripts\\python scripts\\routing_report.py
    venv\\Scripts\\python scripts\\routing_report.py --days 30
    venv\\Scripts\\python scripts\\routing_report.py --session <id>
    venv\\Scripts\\python scripts\\routing_report.py --export-cases new_cases.json

CONFIRMED MISSES are the interesting rows. A turn is one when the chat path
itself signalled that routing should have caught it:

  rescue_fired      the chat model offered a web search it cannot run, and the
                    dead-end backstop had to run the planner for it. A rescue is
                    by definition a WEB turn, so these export as FULLY LABELLED
                    bench cases with no human labelling at all.
  impersonation_cut the chat model fabricated a task/reminder lifecycle. That is
                    what a routing miss looks like from the user's seat; the
                    right label needs a human, so these export as TODO.

--export-cases writes them in scripts/route_cases.json's schema, which is the
loop this whole thing exists to close: production misses become permanent
regression cases.

NEVER collected by pytest (lives outside tests/ and reads the real database).
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import sys
from collections import Counter
from datetime import timedelta
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


def _quiet_sql() -> None:
    """The app engine is built with echo on, and SQLAlchemy raises its own
    logger to INFO when it constructs that engine - so this has to run AFTER
    app.db.database is imported, not at module scope. (It was at module scope
    first, did nothing, and the report shipped buried under the query that
    produced it.)"""
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)


def _pct(n: int, total: int) -> str:
    return f"{(100.0 * n / total):5.1f}%" if total else "    - "


def _percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


def _bar(counter: Counter, total: int, *, indent: str = "  ") -> None:
    width = max([len(str(k)) for k in counter] + [4]) if counter else 4
    for key, n in counter.most_common():
        print(f"{indent}{str(key).ljust(width)}  {n:6}  {_pct(n, total)}")


async def _load(days: int, session: str | None) -> list:
    from sqlalchemy import desc, select

    from app.db.database import AsyncSessionLocal
    from app.db.models import RoutingDecision, utc_now

    _quiet_sql()
    cutoff = utc_now() - timedelta(days=days)
    query = select(RoutingDecision).where(RoutingDecision.created_at >= cutoff)
    if session:
        query = query.where(RoutingDecision.session_id == session)
    async with AsyncSessionLocal() as db:
        result = await db.execute(query.order_by(desc(RoutingDecision.created_at)))
        return list(result.scalars().all())


def _report(rows: list, days: int) -> None:
    total = len(rows)
    print(f"\nrouting decisions - last {days} day(s): {total} turn(s)")
    if not total:
        print("\nNothing recorded. Either the backend has not run since the routing")
        print("trail shipped, or no chat turns have happened in this window.")
        return

    print("\n--- where turns went " + "-" * 40)
    _bar(Counter(r.outcome for r in rows), total)

    fell_open = [r for r in rows if r.fail_open_reason]
    print(f"\n--- why {len(fell_open)} turn(s) fell open to chat " + "-" * 24)
    if fell_open:
        _bar(Counter(r.fail_open_reason for r in fell_open), len(fell_open))
        print("\n  gate_closed      the deterministic gate never fired - a miss here")
        print("                   cannot be fixed downstream")
        print("  classifier_chat  the gate fired and the model judged it conversation")
        print("  classifier_error the model call failed; CHAT is the fail-open default")
    else:
        print("  (none)")

    routed = [r for r in rows if r.label and not r.fail_open_reason]
    print(f"\n--- labels on {len(routed)} routed turn(s) " + "-" * 30)
    if routed:
        _bar(Counter(f"{r.label}/{r.execution}" for r in routed), len(routed))
    else:
        print("  (none)")

    called = [r for r in rows if r.classifier_ms is not None]
    errored = [r for r in called if r.classifier_error]
    print(f"\n--- the classifier " + "-" * 42)
    print(f"  calls           {len(called)} of {total} turns ({_pct(len(called), total)})")
    if called:
        latency = [r.classifier_ms for r in called]
        print(f"  latency         p50 {_percentile(latency, 0.5)}ms   "
              f"p95 {_percentile(latency, 0.95)}ms   max {max(latency)}ms")
        print(f"  failures        {len(errored)} ({_pct(len(errored), len(called))})")
        models = Counter(r.classifier_model or "(unknown)" for r in called)
        print(f"  models          {', '.join(f'{m} ×{n}' for m, n in models.most_common())}")
        if errored:
            print("  recent failures:")
            for r in errored[:5]:
                print(f"    {(r.classifier_error or '')[:100]}")
    code_decided = [r for r in rows if r.bare_navigation]
    if code_decided:
        print(f"  decided in code {len(code_decided)} (bare navigation - no LLM call)")

    misses = [r for r in rows if r.rescue_fired or r.impersonation_cut]
    print(f"\n--- CONFIRMED misses: {len(misses)} " + "-" * 36)
    if misses:
        rescues = [r for r in misses if r.rescue_fired]
        failed_rescues = [r for r in rescues if r.rescue_ok is False]
        fabrications = [r for r in misses if r.impersonation_cut]
        print(f"  web rescues     {len(rescues)}"
              + (f" ({len(failed_rescues)} of them failed)" if failed_rescues else ""))
        print(f"  fabrications    {len(fabrications)}")
        print()
        for r in misses[:20]:
            kind = "rescue" if r.rescue_fired else "fabricated"
            gate = "gate closed" if r.gate_fired is False else f"gate {r.gate_reason}"
            why = r.fail_open_reason or r.outcome
            print(f"  [{kind:10}] {gate:22} {why:16} {r.message[:70]!r}")
    else:
        print("  (none - nothing in this window admitted to a miss)")

    closed = [r for r in rows if r.gate_fired is False]
    if closed:
        print(f"\n--- most recent gate-closed turns (eyeball these) " + "-" * 12)
        for r in closed[:15]:
            print(f"  {r.message[:90]!r}")


def _export(rows: list, path: Path) -> None:
    """Write confirmed misses as route_bench.py cases.

    A rescue is a WEB turn by construction - the backstop ran a web plan for it -
    so those come out fully labelled. A fabrication needs a human to say what it
    should have been, so it comes out with expect.label null and a TODO."""
    cases = []
    for r in rows:
        if not (r.rescue_fired or r.impersonation_cut):
            continue
        case = {
            "id": f"logged-{r.id[:8]}",
            "message": r.message,
            "expect": {"label": "WEB" if r.rescue_fired else None},
            "why": (
                f"logged {r.created_at:%Y-%m-%d}: "
                + ("the chat model offered a web search it cannot run "
                   "(dead-end rescue fired)" if r.rescue_fired
                   else "the chat model fabricated a task lifecycle "
                        "(impersonation guard cut the stream) - TODO: set expect.label")
                + f"; {r.fail_open_reason or r.outcome}"
            ),
        }
        cases.append(case)
    path.write_text(
        json.dumps({"cases": cases}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    todo = sum(1 for c in cases if c["expect"]["label"] is None)
    print(f"\nwritten: {path} - {len(cases)} case(s)"
          + (f", {todo} needing a label" if todo else ""))
    if cases:
        print("Merge the ones you agree with into scripts/route_cases.json.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--session", default=None)
    parser.add_argument("--export-cases", default=None, metavar="PATH")
    args = parser.parse_args()

    try:
        rows = asyncio.run(_load(args.days, args.session))
    except Exception as exc:  # noqa: BLE001
        if "no such table" in str(exc).lower():
            print("\nThe routing_decisions table does not exist yet.")
            print("Start the backend once - migrations run automatically at boot")
            print("(app/db/migrate.py) - then re-run this report.")
            return 2
        raise
    _report(rows, args.days)
    if args.export_cases:
        _export(rows, Path(args.export_cases))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
