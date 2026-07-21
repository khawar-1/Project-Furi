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

# MULTI-COMMIT (15.1): how many approved submits have already fired for this
# browse goal. The planner stamps it alongside COMMIT_PARAM each time it re-arms
# the step for the NEXT form, so each submit's signature is distinct (a fresh,
# separate approval — never the same one replayed). Display/signature only;
# perform() reads the authoritative count off the live held session, not this.
COMMITS_DONE_PARAM = "_commits_done"


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
    # "login" (sign-in) or "signup" (account creation) — both hand off to a
    # user-driven window; only the pause text differs. Default keeps every
    # pre-signup path unchanged.
    wall_kind: str = "login"
    # DISCOVER needed a form value it could not ground in the user's autofill
    # profile or their words (15.2). The planner pauses the plan on a clarifying
    # question naming `fill_field`; the user's answer grounds the value on the
    # resumed discovery. Never a guessed or page-supplied value goes into a form.
    fill_required: bool = False
    fill_field: str = ""
    # DISCOVER hit a CAPTCHA / verification challenge (15.4) it must NEVER solve.
    # Mode-split 2026-07-19:
    #   "interstitial" — the page IS the challenge; the session closed, the
    #     planner opens a user-driven window, the user solves it there (the
    #     profile carries the clearance cookie) and the resumed discovery
    #     starts fresh.
    #   "embedded" — a widget ON the form; its token can never leave the agent's
    #     window, so the live session (form filled, vendor traffic armed) is
    #     HELD in browser_session's challenge registry and the user ticks the
    #     box in the agent's own headed window; the resumed discovery re-attaches
    #     to it. Jarvis detects and waits, never solving the challenge.
    challenge_required: bool = False
    challenge_kind: str = ""
    challenge_site: str = ""
    challenge_mode: str = ""
    # DISCOVER's loop would leave the sites the user named for a page-derived
    # origin (an external application/ATS, 2026-07-18). The planner pauses the
    # plan asking the user to approve THIS origin; only their "yes" adds it and
    # the resumed discovery may reach the off-site form. Jarvis never follows a
    # page-derived site on its own.
    origin_approval_required: bool = False
    origin_candidate: str = ""
    # The exact off-site URL discovery wanted to open — recorded so a "yes" can
    # resume AT the approved page instead of restarting blind (2026-07-19).
    origin_url: str = ""
    # DISCOVER landed on a page that OFFERS an account (sign in / sign up) while
    # the form could proceed as a guest (2026-07-19). Not a wall — the planner
    # asks the user which they want; the live part-filled session is HELD so the
    # window stays open across the pause. `auth_offer_url` is recorded so the
    # planner never re-asks the same page.
    auth_offer_required: bool = False
    auth_offer_signin: bool = False
    auth_offer_signup: bool = False
    auth_offer_site: str = ""
    auth_offer_url: str = ""


async def _load_vision_config():
    """Read the 15.3 browser-vision toggle on its own DB session. Best-effort:
    any failure yields the default (disabled), so a config hiccup keeps the commit
    loop DOM-only rather than failing it. Mirrors browser_agent_tools'
    _load_browser_vision_config — duplicated (5 lines) to keep agents/ off a
    module cycle with tools/."""
    from app.core.app_settings import (
        default_browser_vision_config,
        get_browser_vision_config,
    )
    from app.db.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            return await get_browser_vision_config(db)
    except Exception as exc:
        logger.debug(f"commit vision config read failed: {type(exc).__name__}: {exc}")
        return default_browser_vision_config()


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


async def discover(
    params: dict,
    session_id: Optional[str] = None,
    profile: Any = None,
    fill_grounding: str = "",
    auth_resolved: Optional[set] = None,
) -> CommitDiscovery:
    """DISCOVER phase — drive to the form, fill it, read the exact submit
    contract, and HOLD the live session for the approved submit. Runs entirely on
    the dedicated browser loop (Playwright cannot launch on uvicorn's --reload
    SelectorEventLoop). Never raises: every failure becomes a CommitDiscovery
    with a code-authored `error` the planner can act on.

    `profile` (15.2) is the DB-free autofill snapshot the loop fills forms from;
    `fill_grounding` is the user's own words (conversation + answers) a fill
    value may also trace to. A value the loop can't ground in either → the loop
    stops and this returns fill_required, so the planner asks the user for it
    rather than typing a guess."""
    from app.agents import browser_loop
    from app.core import browser_runtime, browser_session
    from app.core.browser_session import (
        BrowserBlocked,
        BrowserSession,
        BrowserUnavailable,
        _normalize_origin,
    )
    from app.providers.factory import build_provider
    from app.providers.vision import build_vision_provider
    from app.tools.browser_tools import _validate_url

    goal = str(params.get("goal") or "").strip()
    if not goal:
        return CommitDiscovery(error="A goal is required — what form should I fill and submit?")

    start_url, error = _validate_url(str(params.get("start_url") or ""))
    if error:
        return CommitDiscovery(error=error)

    allowlist = _allowlist(params)
    allowlist.add(_normalize_origin(start_url))

    # The 15.3 vision fallback toggle, read on THIS (main) loop; the provider is
    # built inside _run (the browser loop) so its client binds there. Best-effort
    # — a config hiccup stays DOM-only, never fails the discovery.
    vision_config = await _load_vision_config()

    async def _run() -> CommitDiscovery:
        provider = build_provider()
        vision = build_vision_provider(vision_config)
        session = None
        held = False
        try:
            # EMBEDDED-challenge resume (2026-07-19): a prior discovery paused on
            # an unsolved widget and HELD its live session (form filled, the user
            # has since ticked the box in that very window). Re-attach to it —
            # take_challenge() disarmed the vendor carve-out — and do NOT
            # navigate: the solved token is bound to the page as it stands, and
            # session.browse_history carries the loop's context forward.
            # ONLY this goal's own hold is re-attached; a stale hold from an
            # abandoned flow would sit on the wrong page with the wrong
            # allowlist, so it is discarded instead.
            pending = browser_session.pending_challenge()
            pending_disc = browser_session.pending_discovery()
            if pending is not None and pending.get("goal") == goal:
                session = await browser_session.take_challenge()
            elif pending_disc is not None and pending_disc.get("goal") == goal:
                # RE-ATTACH a discovery session HELD across a fill/origin/auth
                # pause (2026-07-19). The window stayed open with the form
                # part-filled; carry on from where it stopped instead of
                # relaunching. The allowlist may have GROWN (an origin the user
                # just approved is now in `allowlist`), so union it in — the
                # interceptor reads self.allowlist live, so this takes effect at
                # once. For an ORIGIN approval we NAVIGATE to the approved URL
                # (the planner stamped it into start_url); for fill/auth we stay
                # on the current page (nothing to navigate to — the answer just
                # unblocks the next fill / the guest choice).
                session = await browser_session.take_discovery()
                if session is not None:
                    try:
                        session.allowlist |= allowlist
                    except Exception:
                        pass
                    if pending_disc.get("reason") == "origin":
                        await session.goto(start_url)
            else:
                await browser_session.discard_challenge()
                await browser_session.discard_discovery()
                session = None
            if session is None:
                # One profile = one live persistent context (the browse rule): a
                # sign-in window, a kept-open result window from a prior submit, OR
                # a kept-open media session all hold the profile lock, so close
                # every one before launching or the launch races the lock.
                await browser_session.close_login_window()
                await browser_session.close_result_window()
                await browser_session.stop_media()
                session = await BrowserSession.open(allowlist)
                await session.goto(start_url)
            # upload_path (14.6): the file the user named to attach, already
            # grounded + path-safety-checked by the planner before this ran. The
            # loop attaches it and folds it into commit_state; None = a plain
            # form with no upload.
            outcome = await browser_loop.run_browse(
                session, goal, provider, commit=True,
                upload_path=str(params.get("upload_path") or "").strip() or None,
                profile=profile,
                fill_grounding=fill_grounding,
                fields=params.get("fields") if isinstance(params.get("fields"), dict) else None,
                vision=vision,
                auth_resolved=set(auth_resolved or set()),
            )

            if outcome.fill_required:
                # A form value could not be grounded in the profile or the user's
                # words (15.2). Not an error to replan — the planner pauses the
                # plan on a clarifying question naming the field, and the answer
                # grounds it on the resumed discovery. HOLD the live, part-filled
                # session across the pause (2026-07-19) so the window stays open
                # and the resumed discovery re-attaches and carries on — before,
                # it closed the window the moment it asked ("filled two fields
                # and then closed the chrome").
                await browser_session.hold_discovery(
                    session, meta={"goal": goal, "reason": "fill"}
                )
                held = True
                return CommitDiscovery(
                    fill_required=True,
                    fill_field=outcome.fill_field or "a form field",
                    error=outcome.error or "I need a value for a form field.",
                )
            if outcome.auth_offer_required:
                # The page OFFERS an account while the form could proceed as a
                # guest (2026-07-19). Not an error to replan — the planner asks
                # the user which they want. HOLD the session across the pause so
                # "apply as guest" resumes right here; a sign-in/up choice
                # discards this hold in the planner (the sign-in window needs the
                # profile lock) and re-runs fresh, authenticated.
                await browser_session.hold_discovery(
                    session, meta={"goal": goal, "reason": "auth"}
                )
                held = True
                return CommitDiscovery(
                    auth_offer_required=True,
                    auth_offer_signin=outcome.auth_offer_signin,
                    auth_offer_signup=outcome.auth_offer_signup,
                    auth_offer_site=outcome.auth_offer_site or "the site",
                    auth_offer_url=outcome.auth_offer_url or "",
                    error=outcome.error or "the site offers sign in / sign up",
                )
            if outcome.login_required:
                kind = outcome.wall_kind or "login"
                site = outcome.login_site or "the site"
                error = (
                    f"account sign-up required at {site} — I can't create an "
                    "account for you; please sign up yourself first."
                    if kind == "signup"
                    else (
                        f"sign-in required at {site} — I can't submit a login "
                        "form; the account has to be signed in first."
                    )
                )
                return CommitDiscovery(
                    login_required=True,
                    login_site=site,
                    wall_kind=kind,
                    error=error,
                )
            if outcome.challenge_required:
                # A human-verification challenge (15.4, mode-split 2026-07-19).
                # Jarvis never solves one, either way.
                kind = outcome.challenge_kind or "CAPTCHA"
                site = outcome.challenge_site or "the site"
                if outcome.challenge_mode == "embedded":
                    # The widget sits ON the form; its token cannot leave this
                    # window. HOLD the live session (form intact), arm the
                    # vendor-only carve-out so the human's solve can complete,
                    # and leave a history note so the resumed loop knows why it
                    # stopped. The planner pauses; the user ticks the box in
                    # THIS window and says continue.
                    session.arm_challenge_traffic()
                    try:
                        session.browse_history.append(
                            f"- paused for the user to complete the {kind} "
                            "verification in this window"
                        )
                    except Exception:
                        pass
                    await browser_session.hold_challenge(
                        session,
                        meta={
                            "kind": kind, "site": site,
                            "url": outcome.challenge_url, "goal": goal,
                        },
                    )
                    held = True  # the registry owns the session now
                    return CommitDiscovery(
                        challenge_required=True,
                        challenge_kind=kind,
                        challenge_site=site,
                        challenge_mode="embedded",
                        error=(
                            f"a {kind} verification at {site} must be completed "
                            "in the open browser window first — I never solve these."
                        ),
                    )
                return CommitDiscovery(
                    challenge_required=True,
                    challenge_kind=kind,
                    challenge_site=site,
                    challenge_mode=outcome.challenge_mode or "interstitial",
                    error=(
                        f"a {kind} verification at {site} must be completed first — "
                        "I never solve these."
                    ),
                )
            if outcome.origin_approval_required:
                # The way to the form leaves the sites the user named for a
                # page-derived origin (an external ATS, 2026-07-18). Not an error
                # to replan — the planner asks the user to approve THIS origin and
                # the resumed discovery reaches the off-site form. Jarvis never
                # follows a page-derived site on its own.
                host = outcome.origin_candidate or "another site"
                # HOLD the live session across the approval pause (2026-07-19) so
                # the window stays open — before, it CLOSED the moment it asked
                # ("it closed chrome before asking my permission"). On "yes" the
                # resumed discovery re-attaches, unions the newly-approved origin
                # into the live allowlist, and navigates to the approved URL.
                await browser_session.hold_discovery(
                    session, meta={"goal": goal, "reason": "origin"}
                )
                held = True
                return CommitDiscovery(
                    origin_approval_required=True,
                    origin_candidate=host,
                    origin_url=outcome.origin_url or "",
                    error=f"needs your approval to visit {host}",
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
            if vision is not None:
                try:
                    await vision.aclose()
                except Exception:
                    pass

    try:
        return await browser_runtime.run_browser(
            _run(), timeout=browser_runtime.BROWSE_HARD_TIMEOUT
        )
    except Exception as exc:
        logger.warning(f"commit discovery marshaling failed: {type(exc).__name__}: {exc}")
        return CommitDiscovery(error=f"Preparing the form failed: {type(exc).__name__}")


async def perform(
    approved: dict,
    *,
    goal: str = "",
    max_commits: int = 1,
    upload_path: Optional[str] = None,
    keep_open: bool = False,
    profile: Any = None,
    fill_grounding: str = "",
    fields: Optional[dict] = None,
    vision_config: Any = None,
) -> dict:
    """SUBMIT phase — the one approved mutation. Takes the held session,
    re-verifies the form is UNCHANGED from what was approved (fail closed), arms
    the interceptor for exactly that request, fires the form's own submit, and
    re-locks. Runs on the dedicated browser loop. Returns a plain result dict;
    the tool wraps it in a ToolResult. Never raises.

    MULTI-COMMIT (15.1). `max_commits` (structurally clamped by the tool, ≥1) is
    how many approved submits ONE browse goal may perform. After a submit that
    FIRED, if the budget is not spent, this RESUMES the SAME live session — now
    read-only again (the one-shot arm was consumed the instant the submit fired,
    so the interceptor aborts every non-GET once more) — to browse on and reach
    the NEXT form. If it reaches one, the session is RE-HELD in the commit
    registry and this returns next_commit_required=True with next_commit_state;
    the planner re-arms the step for a FRESH, SEPARATE approval. Nothing is
    batched, nothing is replayed — every submit is its own signature, its own
    approval, its own one-shot permit, exactly the 14.5 guarantee repeated.
    Because form #2 does not exist until #1 is submitted and the page navigates,
    multi-commit is inherently sequential; the budget bounds the runaway.

    `keep_open`: after the FINAL submit that FIRED, leave the window open
    (registered in browser_session's result-window registry) so the user can SEE
    the site's response, instead of closing it. Safe by construction — the submit
    consumed the one-shot commit arm and the interceptor is still READ-mode (we
    never call enter_playback_mode here), so the lingering window can issue no
    further non-GET; a second submit would need a fresh approval-armed permit. An
    intermediate submit is never kept open — its session is re-held for the next
    form instead. `response_text` (the site's own visible response prose) is
    returned so the completion text is GROUNDED in what the server actually said,
    not the goal."""
    from app.core import browser_runtime, browser_session, dom_observe

    budget = max(1, int(max_commits or 1))
    # The 15.3 vision fallback toggle (read on THIS main loop when the caller did
    # not supply it) — threaded into the multi-commit RESUME so a later form is
    # filled with the same vision assist. Best-effort → DOM-only on any hiccup.
    if vision_config is None:
        vision_config = await _load_vision_config()

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
        handed_off = False   # given to the result-window registry (final submit)
        re_held = False      # re-held for the NEXT form (intermediate submit)
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

            if fired:
                # Count this submit against the budget. The live session is the
                # single source of truth (it travels across the pause); the
                # planner's _commits_done param is only for the signature.
                try:
                    session.commits_done = int(getattr(session, "commits_done", 0) or 0) + 1
                except Exception:
                    session.commits_done = 1
            commits_done = int(getattr(session, "commits_done", 0) or 0)

            result = {
                "submitted": fired,
                "url": summary.get("url", ""),
                "title": summary.get("title", ""),
                "rendered": summary.get("rendered", ""),
                "response_text": response_text[:1500],
                "window_open": False,
                "next_commit_required": False,
                "commits_done": commits_done,
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

            # MULTI-COMMIT (15.1): budget remaining → resume the SAME session
            # (read-only again) to reach the next form. Only after a submit that
            # actually fired; a submission that never went out ends the flow.
            if fired and commits_done < budget:
                nxt = await _resume_for_next_form(
                    session, goal, upload_path, profile, fill_grounding, fields,
                    vision_config,
                )
                if nxt is not None:
                    await browser_session.hold_commit(session, state=nxt)
                    re_held = True   # the registry owns it across the next pause
                    result["next_commit_required"] = True
                    result["next_commit_state"] = nxt
                    return result
                # No further form (done / login wall / couldn't reach one) — this
                # was the last submit; fall through to close/keep-open.

            # Final submit: keep the window OPEN on a real submission so the user
            # can see the result. The registry owns the session now — the finally
            # must NOT close it. Safe: the commit arm is spent and the interceptor
            # stays locked (READ mode), so no second mutation can fire.
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
            # to the result-window registry (a read-only viewer, arm spent) or
            # re-held for the next approved form (a resumed multi-commit flow).
            if not handed_off and not re_held:
                await session.close()

    try:
        return await browser_runtime.run_browser(
            _run(), timeout=browser_runtime.BROWSE_HARD_TIMEOUT
        )
    except Exception as exc:
        logger.warning(f"commit submit marshaling failed: {type(exc).__name__}: {exc}")
        return {"submitted": False, "error": f"The submission failed: {type(exc).__name__}"}


async def _resume_for_next_form(
    session: Any,
    goal: str,
    upload_path: Optional[str],
    profile: Any = None,
    fill_grounding: str = "",
    fields: Optional[dict] = None,
    vision_config: Any = None,
) -> Optional[dict]:
    """Drive the SAME (now read-only again) session onward to the next form for
    the goal. Returns its code-read commit_state ({url, method, fields, uploads})
    when a submittable form is reached, else None (the loop finished, hit a
    sign-in wall, needed an ungrounded value, or could not reach one — all of
    which end the multi-commit flow). Never raises: a failure to reach another
    form is a normal end, not a crash. A separate provider is built here
    (build_provider, not the cached create_provider) so its httpx client binds to
    the browser loop this runs on. The autofill `profile` is threaded through so
    a later form fills from the same grounded data (15.2)."""
    from app.agents import browser_loop
    from app.providers.factory import build_provider
    from app.providers.vision import build_vision_provider

    provider = build_provider()
    vision = build_vision_provider(vision_config)
    try:
        outcome = await browser_loop.run_browse(
            session, goal, provider, commit=True,
            upload_path=(str(upload_path).strip() or None) if upload_path else None,
            profile=profile,
            fill_grounding=fill_grounding,
            fields=fields if isinstance(fields, dict) else None,
            vision=vision,
        )
    except Exception as exc:
        logger.warning(f"multi-commit resume failed: {type(exc).__name__}: {exc}")
        return None
    finally:
        try:
            await provider.__aexit__(None, None, None)  # close its httpx client
        except Exception:
            pass
        if vision is not None:
            try:
                await vision.aclose()
            except Exception:
                pass
    if outcome.commit_required and (outcome.commit_state or {}).get("url"):
        logger.info(
            "browse_commit: reached the next form at "
            f"{str(outcome.commit_state.get('url'))[:120]} — pausing for approval"
        )
        return outcome.commit_state
    return None
