"""Typed browse state — the ONE vocabulary for every way a browser flow can
pause, and the ONE owner of the commit step-parameter stamps.

WHY. The pause taxonomy (login wall, signup wall, optional auth offer, an
ungroundable form value, a page-derived off-site origin, a CAPTCHA, a commit
awaiting approval, the next form of a multi-commit) was re-encoded as ~35
boolean-flag/string fields on BrowseOutcome, ~20 more on CommitDiscovery, and
three ~500-line if-chains reading them (run_browse, discover, the planner's
_execute_node). Every new pause reason meant touching all five places and
hand-copying fields between the two structs. This module gives the taxonomy
one name each (Handoff) and one carrier (HandoffPayload); the flag structs
derive their payload here, so producers and consumers share a single shape.

COMMIT PARAMS. The "_commit"/"_commits_done" step parameters are the approval
binding — their presence flips a browse_commit step from discover to submit,
and they live in `parameters` precisely so signature() binds the user's
approval to the exact code-read contract. Scattered raw dict pokes across
three modules made that invariant unauditable; every WRITE now goes through
the stamp/clear helpers below (reads may stay direct — reading cannot break
the binding). stamp_start_url() additionally enforces the rule a comment used
to carry: a step whose parameters already hold a commit contract is
approval-bound and must NEVER be re-parameterized outside a fresh discovery.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

# The step-parameter key the planner writes the code-read form contract into
# after DISCOVER. Part of signature() by construction (it lives in
# `parameters`), so the approval binds to the exact values the card showed.
# Underscore-prefixed so it never collides with a tool-schema field.
COMMIT_PARAM = "_commit"

# MULTI-COMMIT (15.1): how many approved submits have already fired for this
# browse goal, stamped alongside COMMIT_PARAM at each re-arm so every submit's
# signature is distinct. Display/signature only — perform() reads the
# authoritative count off the live held session, never this.
COMMITS_DONE_PARAM = "_commits_done"


class Handoff(str, Enum):
    """Every way a browser flow stops to hand control to the user. str-valued
    so a payload serializes into parked-plan JSON and push events verbatim."""

    LOGIN = "login"                      # hard sign-in wall
    SIGNUP = "signup"                    # hard account-creation wall
    AUTH_OFFER = "auth_offer"            # optional sign-in/up; guest possible
    FILL_FIELD = "fill_field"            # a form value nothing grounds
    TARGET_CHOICE = "target_choice"      # several page items match equally well
    ORIGIN_APPROVAL = "origin_approval"  # page-derived off-site origin
    ACTION_APPROVAL = "action_approval"  # a world-acting gesture in READ mode
    CHALLENGE = "challenge"              # CAPTCHA — never solved by Jarvis
    COMMIT = "commit"                    # form contract awaiting approval
    NEXT_COMMIT = "next_commit"          # multi-commit: next form ready
    WINDOW_EXPIRED = "window_expired"    # a resume found its held window gone
    SITE_UNRESOLVED = "site_unresolved"  # the named domain does not exist
    STUCK = "stuck"                      # no safe next action — ask, don't die


@dataclass(frozen=True)
class HandoffPayload:
    """The data one pause carries. One type for every reason — consumers
    switch on `reason` instead of probing parallel boolean flags."""

    reason: Handoff
    site: str = ""
    url: str = ""                 # the page the handoff was raised on
    field: str = ""               # FILL_FIELD: the form field's name
    suggested_value: str = ""     # FILL_FIELD: page-derived value, for correction
    origin: str = ""              # ORIGIN_APPROVAL: the normalized candidate
    action_desc: str = ""         # ACTION_APPROVAL: what the loop is about to do
    action_fingerprint: str = ""  # ACTION_APPROVAL: the permit for THAT one gesture
    challenge_kind: str = ""      # CHALLENGE: reCAPTCHA / Cloudflare / hCaptcha
    challenge_mode: str = ""      # CHALLENGE: "interstitial" | "embedded"
    auth_signin: bool = False     # AUTH_OFFER: the page offers sign-in
    auth_signup: bool = False     # AUTH_OFFER: the page offers sign-up
    # LOGIN / SIGNUP / CHALLENGE: the tab was handed to the user IN PLACE rather
    # than a separate window being opened (2026-08-08). WHERE THE USER SHOULD
    # LOOK — the pause text must not say "I've opened a window" about a tab that
    # was already on screen, which is how the live incident's episode-1 page read
    # as Jarvis's answer instead of as the page it had stopped on. One field for
    # every kind, because the question is identical for all of them; the CHALLENGE
    # branch has read `challenge_in_place` since 2026-08-03 and nothing had ever
    # passed it, so it also closes that.
    in_place: bool = False
    # TARGET_CHOICE: which kind of thing is ambiguous ("item" = a product on a
    # listing, "option" = a value in a form control), what the user asked for in
    # their own words, the control's name when it is an option, and the real
    # labels to offer. Options are verbatim page text — see browser/choice.py.
    choice_kind: str = ""
    choice_target: str = ""
    choice_field: str = ""
    choice_options: list = dataclasses.field(default_factory=list)
    # How many matched in TOTAL, which exceeds len(choice_options) once the tie is
    # longer than a question may show (2026-08-08). The pause text reports it, so
    # a shortened list can never be read as the whole answer. 0 = "same as what is
    # shown", which is what a parked payload written before this field says.
    choice_total: int = 0
    # Every offered item is sold out (2026-08-09). Normally the unbuyable ones
    # are simply not offered; this is the case where none survived, so the
    # question has to say none can be added rather than present dead ends.
    # Defaults False — what a payload parked before this field says, and the
    # behaviour that predates it.
    choice_unbuyable: bool = False
    # dataclasses.field spelled out: the attribute named `field` above shadows
    # the bare name inside this class body.
    commit_state: dict = dataclasses.field(default_factory=dict)
    commits_done: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form (parked plans, push events, ToolResult outputs)."""
        return {
            "reason": self.reason.value,
            "site": self.site,
            "url": self.url,
            "field": self.field,
            "suggested_value": self.suggested_value,
            "origin": self.origin,
            "action_desc": self.action_desc,
            "action_fingerprint": self.action_fingerprint,
            "challenge_kind": self.challenge_kind,
            "challenge_mode": self.challenge_mode,
            "auth_signin": self.auth_signin,
            "auth_signup": self.auth_signup,
            "in_place": self.in_place,
            "choice_kind": self.choice_kind,
            "choice_target": self.choice_target,
            "choice_field": self.choice_field,
            "choice_options": list(self.choice_options),
            "choice_total": self.choice_total,
            "choice_unbuyable": self.choice_unbuyable,
            "commit_state": dict(self.commit_state),
            "commits_done": self.commits_done,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Optional["HandoffPayload"]:
        """Inverse of to_dict, tolerant of junk — None rather than a crash
        (parked JSON may predate a field or carry a foreign shape)."""
        try:
            reason = Handoff(str(data.get("reason") or ""))
        except (ValueError, AttributeError):
            return None
        return cls(
            reason=reason,
            site=str(data.get("site") or ""),
            url=str(data.get("url") or ""),
            field=str(data.get("field") or ""),
            suggested_value=str(data.get("suggested_value") or ""),
            origin=str(data.get("origin") or ""),
            action_desc=str(data.get("action_desc") or ""),
            action_fingerprint=str(data.get("action_fingerprint") or ""),
            challenge_kind=str(data.get("challenge_kind") or ""),
            challenge_mode=str(data.get("challenge_mode") or ""),
            auth_signin=bool(data.get("auth_signin")),
            auth_signup=bool(data.get("auth_signup")),
            in_place=bool(data.get("in_place")),
            choice_kind=str(data.get("choice_kind") or ""),
            choice_target=str(data.get("choice_target") or ""),
            choice_field=str(data.get("choice_field") or ""),
            choice_options=[str(o) for o in (data.get("choice_options") or [])],
            choice_total=int(data.get("choice_total") or 0),
            choice_unbuyable=bool(data.get("choice_unbuyable")),
            commit_state=dict(data.get("commit_state") or {}),
            commits_done=int(data.get("commits_done") or 0),
        )


def _wall_reason(wall_kind: str) -> Handoff:
    return Handoff.SIGNUP if (wall_kind or "").strip() == "signup" else Handoff.LOGIN


def handoff_from_outcome(outcome: Any) -> Optional[HandoffPayload]:
    """Derive the payload a BrowseOutcome's flags encode, or None when the run
    simply ended. Precedence mirrors the planner's dispatch order (unresolved
    site → fill → auth offer → wall → challenge → origin → action → commit) so
    flag-probing call sites and payload consumers can never disagree about which
    pause wins."""
    # FIRST, because it is the only reason that means nothing else happened at
    # all: the address named has no DNS record, so there was never a page to
    # hit a wall, a challenge, or a form on. Every other flag would be stale.
    if getattr(outcome, "site_unresolved", False):
        return HandoffPayload(
            reason=Handoff.SITE_UNRESOLVED,
            site=str(getattr(outcome, "unresolved_host", "") or ""),
            url=str(getattr(outcome, "url", "") or ""),
        )
    # BEFORE the fill ask, because the two are the same question asked with and
    # without a list to choose from: when code can show the real options, a
    # pick-list beats "type the value yourself". They are mutually exclusive in
    # practice (one stop sets one flag); the order states which wins if that
    # ever stops being true.
    if getattr(outcome, "target_choice_required", False):
        return HandoffPayload(
            reason=Handoff.TARGET_CHOICE,
            choice_kind=str(getattr(outcome, "choice_kind", "") or "item"),
            choice_target=str(getattr(outcome, "choice_target", "") or ""),
            choice_field=str(getattr(outcome, "choice_field", "") or ""),
            choice_options=[
                str(o) for o in (getattr(outcome, "choice_options", None) or [])
            ],
            choice_total=int(getattr(outcome, "choice_total", 0) or 0),
            choice_unbuyable=bool(getattr(outcome, "choice_unbuyable", False)),
            url=str(getattr(outcome, "url", "") or ""),
        )
    if getattr(outcome, "fill_required", False):
        return HandoffPayload(
            reason=Handoff.FILL_FIELD,
            field=str(getattr(outcome, "fill_field", "") or ""),
            suggested_value=str(getattr(outcome, "fill_value", "") or ""),
            url=str(getattr(outcome, "url", "") or ""),
        )
    if getattr(outcome, "auth_offer_required", False):
        return HandoffPayload(
            reason=Handoff.AUTH_OFFER,
            site=str(getattr(outcome, "auth_offer_site", "") or ""),
            url=str(getattr(outcome, "auth_offer_url", "") or ""),
            auth_signin=bool(getattr(outcome, "auth_offer_signin", False)),
            auth_signup=bool(getattr(outcome, "auth_offer_signup", False)),
        )
    if getattr(outcome, "login_required", False):
        return HandoffPayload(
            reason=_wall_reason(getattr(outcome, "wall_kind", "login")),
            site=str(getattr(outcome, "login_site", "") or ""),
            url=str(getattr(outcome, "login_url", "") or ""),
            # Set by the TOOL, not the loop, so it is absent on a raw
            # BrowseOutcome — getattr's default is the right answer there.
            in_place=bool(getattr(outcome, "login_in_place", False)),
        )
    if getattr(outcome, "challenge_required", False):
        return HandoffPayload(
            reason=Handoff.CHALLENGE,
            site=str(getattr(outcome, "challenge_site", "") or ""),
            url=str(getattr(outcome, "challenge_url", "") or ""),
            challenge_kind=str(getattr(outcome, "challenge_kind", "") or ""),
            challenge_mode=str(getattr(outcome, "challenge_mode", "") or ""),
            in_place=bool(getattr(outcome, "challenge_in_place", False)),
        )
    if getattr(outcome, "origin_approval_required", False):
        return HandoffPayload(
            reason=Handoff.ORIGIN_APPROVAL,
            origin=str(getattr(outcome, "origin_candidate", "") or ""),
            url=str(getattr(outcome, "origin_url", "") or ""),
        )
    if getattr(outcome, "action_approval_required", False):
        return HandoffPayload(
            reason=Handoff.ACTION_APPROVAL,
            action_desc=str(getattr(outcome, "action_description", "") or ""),
            action_fingerprint=str(getattr(outcome, "action_fingerprint", "") or ""),
            site=str(getattr(outcome, "action_site", "") or ""),
            url=str(getattr(outcome, "url", "") or ""),
        )
    if getattr(outcome, "commit_required", False):
        return HandoffPayload(
            reason=Handoff.COMMIT,
            commit_state=dict(getattr(outcome, "commit_state", None) or {}),
        )
    # LAST, and that placement is the whole meaning of this one (2026-08-09).
    # STUCK says "nothing else fired": the loop produced no action on a page
    # that raised no wall, no challenge, no tie and no contract. Anywhere
    # earlier it would shadow a real pause — most damagingly a discovered form,
    # which is a run that SUCCEEDED. Being the fallthrough is also what makes it
    # safe to add at all: every existing flag still wins, so no working path
    # changes shape.
    if getattr(outcome, "stuck_required", False):
        return HandoffPayload(
            reason=Handoff.STUCK,
            # No new field: the page rides `url` like every other payload, and
            # what the loop was trying rides the outcome's own `error`, which
            # `_discovery_from_handoff` already threads through. A payload with
            # nothing new to serialize cannot break a parked plan written before
            # it existed.
            url=str(getattr(outcome, "url", "") or ""),
            site=str(getattr(outcome, "stuck_page", "") or ""),
        )
    return None


def handoff_from_discovery(discovery: Any) -> Optional[HandoffPayload]:
    """CommitDiscovery's flags → payload. CommitDiscovery has no challenge_url
    and carries the challenge site under its own names; otherwise the shape
    (and the precedence) is identical to handoff_from_outcome by design."""
    return handoff_from_outcome(discovery)


def handoff_from_flags(mapping: Any) -> Optional[HandoffPayload]:
    """A browse ToolResult's structured output dict (its keys mirror the
    BrowseOutcome field names) → payload. Non-dict input is simply no handoff."""
    if not isinstance(mapping, dict):
        return None
    import types

    return handoff_from_outcome(types.SimpleNamespace(**mapping))


# ------------------------------------------------------------- commit params
def commit_contract(parameters: dict) -> Optional[dict]:
    """The stamped code-read form contract, or None (step still in discovery)."""
    contract = parameters.get(COMMIT_PARAM)
    return contract if isinstance(contract, dict) else None


def commits_done(parameters: dict) -> int:
    try:
        return int(parameters.get(COMMITS_DONE_PARAM) or 0)
    except (TypeError, ValueError):
        return 0


def stamp_commit_contract(
    parameters: dict, contract: dict, *, done: Optional[int] = None
) -> None:
    """THE writer for the approval-binding params. `done` is stamped only when
    given (a first discovery leaves it absent; a multi-commit re-arm records
    how many submits already fired so each re-arm's signature is distinct)."""
    parameters[COMMIT_PARAM] = contract
    if done is not None:
        parameters[COMMITS_DONE_PARAM] = int(done)


def clear_commit_params(parameters: dict) -> None:
    """Drop any stale contract (a replanned/revised step must re-discover —
    an old contract must never ride into a new signature)."""
    parameters.pop(COMMIT_PARAM, None)
    parameters.pop(COMMITS_DONE_PARAM, None)


def stamp_start_url(parameters: dict, url: str) -> bool:
    """Point a PRE-DISCOVERY browse step at an approved URL. REFUSES a step
    whose parameters already carry a commit contract: that step is
    approval-bound (the contract is in its signature), and re-parameterizing
    it would change what the user approved without a fresh approval. The old
    code carried this rule as a comment ("the step is READ/pre-discovery, so
    re-parameterizing re-approves nothing") with nothing enforcing it."""
    if commit_contract(parameters) is not None:
        return False
    parameters["start_url"] = url
    return True
