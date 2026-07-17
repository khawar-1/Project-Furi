"""
Jarvis OS — Evidence escalation (Phase 6 Part 1 hardening, 2026-07-16)

When a web_search comes back with thin evidence, CODE adds the read_webpage
step that gets the real page — no LLM call, no replan budget.

WHY THIS IS CODE AND NOT A PROMPT RULE
--------------------------------------
Planner rule 16 already says: "When the returned snippets do not already
contain a confident answer, follow up with read_webpage on the most promising
result url." It has never once executed. It cannot:

  - the planner drafts ALL steps in _plan_node, BEFORE any step has run, so at
    the only moment the rule is read, the thing it tests ("do the snippets
    answer the question?") does not exist yet; and
  - _after_execute routes back to `revise` only on pause_reason == "failed_step"
    (or an approval pause with unresolved placeholders). A step that SUCCEEDS
    with thin results is not a failure, so the graph never gives the LLM a
    second look at it.

The rule's predicate is unbound at evaluation time. That is not a prompt that
needs strengthening — it is a rule whose condition is unobservable when it is
read. It needs a different evaluation TIME, which is what this module is: the
check runs the instant a web_search transitions to COMPLETED, when the snippets
finally exist.

LIVE INCIDENT (2026-07-16)
--------------------------
"…and which teams are going to fifa finals 2026" searched, got FIFA's
qualified-teams page, kept 300 characters of it — cut mid-word at
"## FIFA World Cup 2026™ qualified t", exactly where the team list began — and
never fetched the page. The summary LLM, handed a heading with no data under
it, invented 112 countries (India, Brunei, Timor-Leste, Seychelles…) for a
48-team tournament and credited them to FIFA. The fix is layered; this is the
layer that makes sure the evidence is actually THERE.

WHY NOT "THIN RESULT = FAILED TOOL RESULT"
------------------------------------------
That looks cheaper (it reuses the replan loop and the _missing_target
precedent) and is a trap, for four independent reasons:
  1. _render_step returns None for any non-COMPLETED step and _fail() sets
     output=None — so failing the search DELETES its evidence from both the
     summary record and the revise prompt. It replaces a thin record with an
     empty one.
  2. MAX_REPLANS = 2. Three thin searches in one turn = three failures = the
     whole turn dies, when today it at least answers two of the three questions.
  3. A search that WORKED would show the user a red FAILED row.
  4. Points 3 and 4 are precisely the incident placeholder_resolver.py was
     written to undo ("spurious red 'failed' steps", "burned replan budget").
So: the search stays COMPLETED and keeps its evidence; we simply go and get
more. This is placeholder_resolver's architecture — resolve in CODE at
execution time, never as an LLM replan. The URL to fetch is already sitting in
the completed step's own output; there is nothing to decide, only to do.

SAFETY
------
The spliced step is an ordinary read_webpage running through execute_tool, so
the SSRF guard applies and the fetch is audited in ActivityLog like any other.
Its permission level comes from the registry, never from here. Nothing bypasses
anything: escalation can only ever cause a READ.
"""
from typing import Optional

from loguru import logger

from app.core.base_tool import PermissionLevel
from app.agents.schemas import AgentPlan, PlanStep, StepStatus
from app.tools.browser_tools import normalize_url
from app.tools.registry import registry

# A result whose extracted content is shorter than this taught us nothing the
# answer can rest on — a title and a teaser, not evidence.
WEB_THIN_CONTENT_CHARS = 600

# Bounds worst-case added latency to ~3 fetches per plan.
MAX_WEB_ESCALATIONS = 3

_SEARCH_TOOL = "web_search"
_READ_TOOL = "read_webpage"


def _rows(step: PlanStep) -> list[dict]:
    output = step.result.output if step.result else None
    if not isinstance(output, dict):
        return []
    rows = output.get("results")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _row_is_complete(row: dict) -> bool:
    """True when this result alone is a substantive, WHOLE extract: long enough
    to answer from, and not cut by us on the way in."""
    return (
        len(str(row.get("content") or "")) >= WEB_THIN_CONTENT_CHARS
        and not row.get("truncated")
    )


def _needs_more(rows: list[dict]) -> bool:
    """True when the search's own results are KNOWN-INCOMPLETE — i.e. not one
    result is both substantive and whole. Two ways to be incomplete:

      thin      — the source gave us little (a title and a teaser);
      truncated — the source gave us plenty and WE cut it.

    Both mean the record admits it is missing something, and in both cases the
    remedy is the same: go read the page.

    The first cut of this module treated ONLY thin as a trigger, reasoning that
    a truncated row still carries a full CONTENT_MAX_CHARS and is therefore
    "plenty". Live verification (2026-07-16, the FIFA re-run) falsified that:
    every source came back truncated at 1200 chars, nothing escalated, and the
    summary honestly reported "the data is cut off in each source, so I can only
    report what was actually retrieved" — a partial answer where the real one
    was two paragraphs further down the page. A cut before the data is a gap
    whatever its size; 1200 chars of preamble answers no better than 300 did.

    Structural signals only. Whether the evidence answers the QUESTION is
    semantic — that would take an LLM, which is the cost this module exists to
    avoid."""
    if not rows:
        return False  # zero results is already a FAILED ToolResult upstream
    return not any(_row_is_complete(r) for r in rows)


def _candidate_urls(rows: list[dict]) -> list[str]:
    """Usable http(s) urls in rank order. (_validate_url in the tool is still the
    authority; this only avoids splicing an obviously junk step.)"""
    urls: list[str] = []
    for r in rows:
        url = str(r.get("url") or "").strip()
        if url.startswith(("http://", "https://")):
            urls.append(url)
    return urls


def _already_targeted(plan: AgentPlan, url: str) -> bool:
    """Idempotence: never fetch a url this plan already reads — including one a
    read already FAILED on, which would fail again. Compared on the normalized
    url so a trailing-slash variant is not treated as a new page."""
    target = normalize_url(url)
    return any(
        s.tool == _READ_TOOL and normalize_url(str(s.parameters.get("url") or "")) == target
        for s in plan.steps
    )


def _readings(rows: list[dict]) -> list[str]:
    """The distinct readings (fan-out queries) these rows came from, in rank
    order. Empty when the search ran a single query — every caller treats that
    as "one reading", never as a reason to skip."""
    out: list[str] = []
    for r in rows:
        for q in r.get("found_by") or []:
            if str(q) not in out:
                out.append(str(q))
    return out


def _rows_of_reading(rows: list[dict], reading: str) -> list[dict]:
    return [r for r in rows if reading in (r.get("found_by") or [])]


def read_gave_nothing(step: PlanStep) -> bool:
    """True when a read_webpage step left its reading no better evidenced.

    A 403 and a 200 carrying a JavaScript shell are the SAME event here: the
    page yielded nothing to answer from. Only the 403 was ever treated that way.
    Live 2026-07-17: a fan-out ranked a YouTube WATCH page first, the escalation
    read it, the read "succeeded" with a player stub, and because it had not
    FAILED it counted as covering the reading — so the retry never ran, the
    qualification reading kept only its teasers, and the summary invented into
    the gap. Reading a video page for prose is a category error, not bad luck.

    A read still PENDING or RUNNING is not judged: it may yet deliver, and
    treating it as dead would splice a duplicate for the same reading."""
    if step.status == StepStatus.FAILED:
        return True
    if step.status != StepStatus.COMPLETED:
        return False
    output = step.result.output if step.result else None
    content = output.get("content") if isinstance(output, dict) else None
    return len(str(content or "")) < WEB_THIN_CONTENT_CHARS


def _reading_is_covered(plan: AgentPlan, rows: list[dict]) -> bool:
    """True when some page belonging to this reading is already being read AND
    that read has not come back empty-handed.

    This distinction is the whole point of separating this from
    _already_targeted. Live 2026-07-17: the ESPN bracket 403'd, and because the
    failed step still sat in plan.steps carrying that url, a url-only check
    considered the reading handled — escalation stopped and the turn answered
    from snippets alone (correctly, by luck). A url we must not re-fetch and a
    reading we have actually covered are different questions."""
    urls = {normalize_url(u) for u in _candidate_urls(rows)}
    return any(
        s.tool == _READ_TOOL
        and not read_gave_nothing(s)
        and normalize_url(str(s.parameters.get("url") or "")) in urls
        for s in plan.steps
    )


def _pending_url(plan: AgentPlan, rows: list[dict]) -> Optional[str]:
    """The next page worth fetching, or None when every reading is served.

    Sufficiency is judged PER READING, not over the whole pile. With a fan-out
    the top reading can come back whole while another has nothing but teasers,
    and a global test would call that "fine" and leave the second interpretation
    unevidenced — which is exactly the question the user may have meant. Still
    structural only: thin/truncated, never "does this answer it?".

    Returning one url at a time (rather than a list) is what makes this
    idempotent: each spliced read covers its reading, so calling again walks to
    the next uncovered one and eventually returns None."""
    readings = _readings(rows)
    if not readings:
        # Single-query search: one reading, judged exactly as it always was.
        if not _needs_more(rows):
            return None
        return next((u for u in _candidate_urls(rows) if not _already_targeted(plan, u)), None)

    for reading in readings:
        subset = _rows_of_reading(rows, reading)
        if not _needs_more(subset):
            continue                       # this reading already has a whole page
        if _reading_is_covered(plan, subset):
            continue                       # a live read for it is already queued
        url = next((u for u in _candidate_urls(subset) if not _already_targeted(plan, u)), None)
        if url is not None:
            return url
    return None


def escalate(plan: AgentPlan, index: int, max_steps: int) -> Optional[PlanStep]:
    """Given the step that just COMPLETED at `index`, return a read_webpage step
    to splice after it, or None.

    Best-effort by construction: any unexpected shape returns None. Enriching
    evidence must never be able to break a plan that already succeeded."""
    try:
        step = plan.steps[index]
        if step.tool != _SEARCH_TOOL or step.status != StepStatus.COMPLETED:
            return None
        # Never recursive: only a search escalates, and the read it produces
        # never escalates in turn.
        if sum(1 for s in plan.steps if s.auto_escalated) >= MAX_WEB_ESCALATIONS:
            return None
        if len(plan.steps) >= max_steps:
            return None

        rows = _rows(step)
        url = _pending_url(plan, rows)
        if url is None:
            return None

        tool = registry.get(_READ_TOOL)
        if tool is None:  # tool not registered — degrade silently
            return None

        logger.info(
            f"Thin web evidence for '{step.parameters.get('query', '')[:60]}' "
            f"→ auto-escalating read_webpage({url}) — no LLM call"
        )
        return PlanStep(
            description=f"Read the full page at {url} for the details the search snippets lack",
            tool=_READ_TOOL,
            parameters={"url": url},
            # Both derived from the registry exactly as _draft_to_steps does —
            # never asserted here. read_webpage is READ today; if that ever
            # changed, this step would correctly start requiring approval
            # rather than silently escalating its own privileges.
            permission_level=tool.permission_level,
            requires_approval=tool.permission_level != PermissionLevel.READ,
            auto_escalated=True,
        )
    except Exception as e:  # noqa: BLE001 — enrichment must never break execution
        logger.warning(f"Evidence escalation skipped: {type(e).__name__}: {e}")
        return None


def escalate_after_failed_read(
    plan: AgentPlan, failed_index: int, max_steps: int
) -> Optional[PlanStep]:
    """An auto-escalated read came back with nothing — try the next candidate
    for its reading.

    Big sites block scrapers, and a 403 on the top result is ordinary, not
    exceptional. Live 2026-07-17: "which teams are playing fifa final 2026"
    escalated to ESPN's bracket, ESPN returned 403, and escalation ended there —
    the answer came from snippets alone and happened to be right, while the reply
    still cited "the bracket data from ESPN", a page it never read. One fetch
    away from having nothing.

    Called on BOTH dead-read paths — a step that failed, and one that completed
    carrying nothing usable (see read_gave_nothing, which self-gates this so a
    substantive read never triggers a pointless retry). The two are the same
    event: the reading is still unevidenced.

    The retry costs no LLM call and no replan budget, and stays under the same
    MAX_WEB_ESCALATIONS bound as any other escalation (the dead step counts
    toward it, so a site that keeps refusing cannot spin)."""
    try:
        failed = plan.steps[failed_index]
        if not failed.auto_escalated or failed.tool != _READ_TOOL:
            return None
        if not read_gave_nothing(failed):
            return None   # it delivered — nothing to retry
        # The search this read was spliced from — the nearest completed one
        # above it. Splices only ever go BELOW their search, so scanning back is
        # exact even after several of them shifted the indices.
        for i in range(failed_index - 1, -1, -1):
            candidate = plan.steps[i]
            if candidate.tool == _SEARCH_TOOL and candidate.status == StepStatus.COMPLETED:
                return escalate(plan, i, max_steps)
        return None
    except Exception as e:  # noqa: BLE001 — a retry must never break execution
        logger.warning(f"Evidence retry skipped: {type(e).__name__}: {e}")
        return None
