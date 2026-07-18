"""
Jarvis OS — Browser COMMIT orchestration (Phase 14, Part 5)

The security-critical half of browser control: submitting ONE form the user has
explicitly approved, and nothing else. It is built as two phases with the
existing signature-approval gate between them:

  DISCOVER (READ)  drive a real browser toward the goal and FILL the form, then
                   read its exact contract — the action URL, the method, and
                   every field value that would be sent. Submit NOTHING (the
                   session's interceptor still aborts every non-GET). Hold the
                   live, filled session in a registry so it survives the pause.

  ── the planner stamps that code-read form state into the step's parameters (so
     signature() binds the approval to the real values) and its action_detail
     (so the card shows exactly what is sent), then PAUSES for signature approval
     exactly as any write step does ──

  SUBMIT (DESTRUCTIVE)  take the held session, RE-VERIFY the form still matches
                   what was approved (fail closed on any change), arm the
                   interceptor for that one request, fire the form's own submit,
                   and re-lock. The one approved non-GET goes out; a second finds
                   no permit.

WHY THIS SHAPE
--------------
The form contract is only knowable AFTER a live browser has navigated to the form
and filled it — it cannot be rendered at plan time. This is the 14.4 pattern
(the tool discovers state, returns a structured signal, the planner pauses) fused
with the Phase 3 signature-approval gate: the discovered values go INTO the
step's parameters, so the ordinary signature machinery — approve exactly what you
saw, replanned steps re-approve — binds the approval to them for free.

HONEST LIMITS (read before trusting this)
-----------------------------------------
- CLASSIC HTML FORMS ONLY. read_commit_target reads a real <form>'s action /
  method / fields. A single-page app that submits by a background fetch to an API
  endpoint has no such contract in the DOM — the loop cannot fill or read it, and
  the submit would be aborted (no matching arm). That is the safe direction
  (nothing sent), but it means commit does not cover every "send" button on the
  web. Same class of limit as "non-GET = mutation" — a strong bound on the
  dominant case, not a universal proof.
- THE HELD SESSION IS MEMORY-ONLY. A backend restart between discovery and
  approval drops it; the submit then reports the session expired and sends
  nothing. This is deliberate — a running Chromium page is not serializable, and
  pretending otherwise is where double-submit lives (the deferred-14.5 note).
- A CANCELLED approval leaves the discovered window held until the next commit
  discovery (which closes it), a discard_commit(), or shutdown — bounded to one
  window, the same lifetime shape as a kept-open media window.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

# The step-parameter key the planner writes the code-read form contract into
# after DISCOVER. Its presence flips a browse_commit step from "discover" to
# "submit", and because it lives in `parameters` it is part of signature() — so
# the approval binds to the exact values the card showed. Underscore-prefixed so
# it never collides with a tool-schema field the LLM fills.
COMMIT_PARAM = "_commit"


@dataclass
class CommitDiscovery:
    """The result of the DISCOVER phase. `state` is the code-read form contract
    ({url, method, fields}) to approve; `error` is a code-authored reason the
    discovery could not reach a submittable form (the planner fails the step
    into its replan loop). Exactly one is set."""

    state: Optional[dict] = None
    error: str = ""
    login_required: bool = False
    login_site: str = ""


def _allowlist(params: dict) -> set[str]:
    from app.core.browser_session import _normalize_origin

    raw = params.get("allowed_origins") or []
    if isinstance(raw, str):
        raw = [raw]
    allow = {o for o in raw if str(o).strip()}
    start = str(params.get("start_url") or "").strip()
    if start:
        allow.add(_normalize_origin(start))
    return allow


async def discover(params: dict, session_id: Optional[str] = None) -> CommitDiscovery:
    """DISCOVER phase — drive to the form, fill it, read the exact submit
    contract, and HOLD the live session for the approved submit. Runs entirely on
    the dedicated browser loop (Playwright cannot launch on uvicorn's --reload
    SelectorEventLoop). Never raises: every failure becomes a CommitDiscovery
    with a code-authored `error` the planner can act on."""
    from app.agents import browser_loop
    from app.core import browser_runtime, browser_session
    from app.core.browser_session import (
        BrowserBlocked,
        BrowserSession,
        BrowserUnavailable,
        _normalize_origin,
    )
    from app.providers.factory import build_provider
    from app.tools.browser_tools import _validate_url

    goal = str(params.get("goal") or "").strip()
    if not goal:
        return CommitDiscovery(error="A goal is required — what form should I fill and submit?")

    start_url, error = _validate_url(str(params.get("start_url") or ""))
    if error:
        return CommitDiscovery(error=error)

    allowlist = _allowlist(params)
    allowlist.add(_normalize_origin(start_url))

    async def _run() -> CommitDiscovery:
        provider = build_provider()
        session = None
        held = False
        try:
            # One profile = one live persistent context (the browse rule): a
            # sign-in window OR a kept-open result window from a prior submit
            # would hold the profile lock, so close both before launching.
            await browser_session.close_login_window()
            await browser_session.close_result_window()
            session = await BrowserSession.open(allowlist)
            await session.goto(start_url)
            # upload_path (14.6): the file the user named to attach, already
            # grounded + path-safety-checked by the planner before this ran. The
            # loop attaches it and folds it into commit_state; None = a plain
            # form with no upload.
            outcome = await browser_loop.run_browse(
                session, goal, provider, commit=True,
                upload_path=str(params.get("upload_path") or "").strip() or None,
            )

            if outcome.login_required:
                return CommitDiscovery(
                    login_required=True,
                    login_site=outcome.login_site or "the site",
                    error=(
                        f"sign-in required at {outcome.login_site or 'the site'} — "
                        "I can't submit a login form; the account has to be signed "
                        "in first."
                    ),
                )
            if not outcome.commit_required or not outcome.commit_state.get("url"):
                return CommitDiscovery(
                    error=(
                        outcome.error
                        or "I couldn't find a form to fill and submit for this goal."
                    )
                )

            await browser_session.hold_commit(session, state=outcome.commit_state)
            held = True  # the registry owns the session now — do NOT close it
            return CommitDiscovery(state=outcome.commit_state)
        except BrowserUnavailable as exc:
            return CommitDiscovery(error=str(exc))
        except BrowserBlocked as exc:
            return CommitDiscovery(error=str(exc))
        except Exception as exc:
            logger.warning(f"commit discovery failed for '{goal[:80]}': {type(exc).__name__}: {exc}")
            return CommitDiscovery(
                error=f"Preparing the form failed: {type(exc).__name__}: {str(exc)[:200]}"
            )
        finally:
            if session is not None and not held:
                await session.close()
            try:
                await provider.__aexit__(None, None, None)  # close its httpx client
            except Exception:
                pass

    try:
        return await browser_runtime.run_browser(_run())
    except Exception as exc:
        logger.warning(f"commit discovery marshaling failed: {type(exc).__name__}: {exc}")
        return CommitDiscovery(error=f"Preparing the form failed: {type(exc).__name__}")


async def perform(approved: dict, *, keep_open: bool = False) -> dict:
    """SUBMIT phase — the one approved mutation. Takes the held session,
    re-verifies the form is UNCHANGED from what was approved (fail closed), arms
    the interceptor for exactly that request, fires the form's own submit, and
    re-locks. Runs on the dedicated browser loop. Returns a plain result dict;
    the tool wraps it in a ToolResult. Never raises.

    `keep_open`: after a submit that FIRED, leave the window open (registered in
    browser_session's result-window registry) so the user can SEE the site's
    response, instead of closing it. Safe by construction — the submit consumed
    the one-shot commit arm and the interceptor is still READ-mode (we never call
    enter_playback_mode here), so the lingering window can issue no further non-GET;
    a second submit would need a fresh approval-armed permit. See
    browser_session.register_result_window for the full argument. `response_text`
    (the site's own visible response prose) is returned either way so the
    completion text is GROUNDED in what the server actually said, not the goal."""
    from app.core import browser_runtime, browser_session, dom_observe

    async def _run() -> dict:
        session = await browser_session.take_commit()
        if session is None:
            return {
                "submitted": False,
                "error": (
                    "The browser session for this submission expired (a restart or "
                    "timeout) — nothing was sent. Ask me to do it again."
                ),
            }
        handed_off = False
        try:
            if not await session.verify_commit(approved):
                return {
                    "submitted": False,
                    "error": (
                        "The web form changed since you approved it — refusing to "
                        "submit. Ask me to prepare it again."
                    ),
                }
            session.arm_commit(
                str(approved.get("method") or "POST"),
                str(approved.get("url") or ""),
            )
            await session.submit_commit()
            await session.settle()

            fired = session.commit_fired()
            response_text = ""
            try:
                observation = await dom_observe.observe(session.page)
                summary = dom_observe.summarize(observation)
                # The page's own visible prose — the "File Uploaded! / <name>"
                # text — clipped. This is what grounds the confirmation.
                response_text = str(getattr(observation, "page_text", "") or "").strip()
            except Exception as exc:
                logger.debug(f"commit post-observe: {type(exc).__name__}: {exc}")
                summary = {"url": "", "title": "", "rendered": ""}

            result = {
                "submitted": fired,
                "url": summary.get("url", ""),
                "title": summary.get("title", ""),
                "rendered": summary.get("rendered", ""),
                "response_text": response_text[:1500],
                "window_open": False,
                "blocked": session.stats.as_dict(),
                "error": (
                    ""
                    if fired
                    else (
                        "The submission did not go through — the site did not send "
                        "the approved request (it may submit by a mechanism this "
                        "tool can't drive). Nothing was sent."
                    )
                ),
            }

            # Keep the window OPEN on a real submission so the user can see the
            # result. The registry owns the session's lifetime now — the finally
            # below must NOT close it. Safe: the commit arm is spent and the
            # interceptor stays locked (READ mode), so no second mutation can fire.
            if keep_open and fired:
                await browser_session.register_result_window(
                    session, title=result["title"], url=result["url"]
                )
                handed_off = True
                result["window_open"] = True
            return result
        finally:
            # One submit per held session, ever: close it here so the approval can
            # never be replayed against a lingering window — UNLESS it was handed
            # to the result-window registry (a read-only viewer with the arm
            # already spent), which owns it until the user closes it.
            if not handed_off:
                await session.close()

    try:
        return await browser_runtime.run_browser(_run())
    except Exception as exc:
        logger.warning(f"commit submit marshaling failed: {type(exc).__name__}: {exc}")
        return {"submitted": False, "error": f"The submission failed: {type(exc).__name__}"}
