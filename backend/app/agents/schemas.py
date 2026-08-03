"""
Jarvis OS — Agent Plan Schemas (Phase 3, Part 4)
Pydantic models for plans, steps, and the LLM planner's raw JSON output.

Trust boundary: the LLM only ever supplies description / tool / parameters
(the *Draft models). Permission levels and requires_approval are ALWAYS
derived in code from the tool registry — a plan can never talk its way
into a lower permission level.
"""
import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.base_tool import PermissionLevel, ToolResult


def _uuid() -> str:
    return str(uuid.uuid4())


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"  # transient: set only while execute_tool is in flight
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"  # never ran: user cancelled the plan, or the step's
    # placeholder source found zero files (nothing to do — an outcome)


class PlanStatus(str, Enum):
    EXECUTING = "executing"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_CHOICE = "awaiting_choice"  # paused on a clarifying question
    PAUSED = "paused"  # stopped BY THE USER mid-run, holding for their steer.
    # Distinct from CANCELLED in exactly one way that matters: the pending
    # steps stay PENDING (see interruption.apply_pause), so the plan can be
    # continued or replanned instead of re-asked from scratch.
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlanQuestion(BaseModel):
    """A clarifying question the planner needs answered before it can continue
    ("three files are named notes.txt — which one?"). Answering a question
    never executes anything: the answer only feeds the next planning round,
    and any write/destructive step still pauses for approval afterwards.

    `kind` tags a question that is not an ordinary clarification so the UI can
    render it distinctly: "login"/"signup" mark a credential handoff where the
    USER signs in / creates the account in the opened window (Jarvis never enters
    the credentials). Empty = a plain clarifying question.

    `about_host` is set only on a "did you mean…?" (kind "site_correction") and
    carries the address that does NOT exist, so the node that parks the question
    can arm the deterministic answer path (_match_site_choice) without re-reading
    it out of the prose. Internal routing, never rendered."""

    text: str
    options: list[str] = Field(default_factory=list)
    kind: str = ""
    about_host: str = ""


class PlanStep(BaseModel):
    """One tool invocation in a plan. permission_level and requires_approval
    are derived from the registry, never taken from the LLM."""

    id: str = Field(default_factory=_uuid)
    description: str
    tool: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    permission_level: PermissionLevel
    requires_approval: bool
    status: StepStatus = StepStatus.PENDING
    result: Optional[ToolResult] = None
    # Code-derived, verbatim rendering of WHAT this step will do (the exact
    # command / paths from the parameters). Shown to the user alongside the
    # LLM's description so an approval can never rest on prose alone.
    action_detail: Optional[str] = None
    # True when CODE added this step to enrich thin evidence, not the LLM
    # (evidence_resolver, 2026-07-16). Such a step is OPPORTUNISTIC: the goal
    # does not depend on it, so its failure must NOT drive the replan loop —
    # the step it enriches already succeeded. Excluded from signature() by
    # construction, so it can never affect what the user approved; defaulted,
    # so plans parked before this field existed still deserialize.
    auto_escalated: bool = False
    # True when CODE widened this web_search from one query to several, because
    # an independent reading enumeration found the goal ambiguous and the draft
    # had committed to a single reading (reading_enumerator, 2026-07-17).
    # Observability, not control: it is the ONLY way to tell a model-authored
    # fan-out from a code-authored one, and so the only way to ever measure
    # whether plan rule 16 does anything. Excluded from signature() by
    # construction; defaulted, so plans parked before this field deserialize.
    auto_fanout: bool = False
    # MULTI-COMMIT flow record (15.5): one entry per approved form this
    # browse_commit step has SUBMITTED so far — {n, url, title, response_text}
    # from each fired submit. It ACCUMULATES across the approval pauses (the
    # step object is re-armed and re-parked between commits, so its result is
    # cleared each time — this is where the per-commit server responses survive),
    # and the completion folds it into result.output["commit_history"] so the
    # grounded flow summary can quote each server response, not just the last.
    # SERIALIZED (rides the parked payload); excluded from signature() by
    # construction; defaulted, so plans parked before this field deserialize.
    browse_commits: list[dict[str, Any]] = Field(default_factory=list)

    def signature(self) -> str:
        """Stable identity of WHAT this step does — used to check that an
        executing step is exactly one the user approved."""
        return json.dumps([self.tool, self.parameters], sort_keys=True, default=str)


class AgentPlan(BaseModel):
    """A full plan: the goal, its ordered steps, and lifecycle status."""

    id: str = Field(default_factory=_uuid)
    goal: str
    session_id: Optional[str] = None
    # Set when this plan belongs to a background Task (Phase 4, Part 5).
    # SERIALIZED (unlike the planner inputs below): the parked payload keeps
    # it, /api/agent/approve routes on it, and the frontend renders
    # "running in background" from it.
    task_id: Optional[str] = None
    steps: list[PlanStep] = Field(default_factory=list)
    status: PlanStatus = PlanStatus.EXECUTING
    message: Optional[str] = None  # failure reason / user-facing note
    created_at: datetime = Field(default_factory=datetime.now)
    # Recent chat turns the planner was given (task_router). Rides along on
    # the parked plan so a replan AFTER approval keeps the same context.
    # Excluded from serialization — it is planner input, not plan output.
    conversation: str = Field(default="", exclude=True)
    # Rendered long-term memory context (Phase 3.5 — "one brain"): what the
    # chat path would know about the user/contacts, given to every planner
    # prompt as data-never-instructions. Rides the parked plan like
    # `conversation`, and is excluded for the same reason.
    memory_context: str = Field(default="", exclude=True)
    # The open clarifying question when status == AWAITING_CHOICE. Serialized:
    # the frontend renders it as clickable options.
    question: Optional[PlanQuestion] = None
    # The user's answers so far — fed into every later planning prompt.
    # Excluded like `conversation`: planner input, not plan output.
    user_answers: list[str] = Field(default_factory=list, exclude=True)
    # How many questions this plan has asked (capped — see MAX_QUESTIONS).
    questions_asked: int = Field(default=0, exclude=True)
    # When the goal reads more than one way, the reading the user most likely
    # meant — decided by reading_enumerator (one job, today's date in view),
    # NOT by the model writing the answer (reading_enumerator, 2026-07-17).
    # SERIALIZED deliberately, unlike the planner inputs above: it is a
    # judgement this plan committed to and acted on, so it belongs in the
    # parked payload (the summary runs long after a background task's plan
    # left memory) and is fair to audit. Empty = the goal had one reading, or
    # we could not tell — both mean "leave the summary's own judgement alone".
    primary_reading: str = ""
    # A replanner looked at the executed results and declared the goal already
    # met ("goal_accomplished": true with an empty revision). SERIALIZED for
    # the same reason as primary_reading: a verdict this plan acted on.
    # It is what lets a plan complete honestly while still CARRYING a failed
    # step — the one case planner._unrouted_failure's positional test cannot
    # see, because when nothing more needs doing, nothing runs after the
    # failure. Defaulted, so plans parked before this field deserialize.
    goal_accomplished: bool = False
    # The domain agent that owns this plan (the boss+agents model): "file" /
    # "email" / "calendar" / "research" / "browser" / "general". SERIALIZED
    # (unlike the planner inputs above) so a paused task's resume rebuilds the
    # SAME specialized planner (its tool subset + persona) from the parked
    # payload — a background browser task must not resume as the general agent.
    # Defaulted so plans parked before this field deserialize as "general".
    # Not a signature input (observability, never a gate decision).
    agent_key: str = "general"
    # Off-site navigation hand-off (2026-07-18): page-derived origins the USER
    # explicitly approved visiting (a job board's 'Apply' link to an external
    # ATS, &c.). A browse loop never follows a page-derived site on its own — it
    # pauses and asks; only a clear "yes" adds the origin here, and this list is
    # then (a) folded into the browse grounding corpus (_browse_grounding) and
    # (b) injected into every browse/browse_commit step's allowed_origins in code
    # (_inject_approved_origins) so the re-drafted step may reach it. SERIALIZED
    # (rides the parked payload across the approval/answer pause); defaulted so
    # plans parked before this field deserialize.
    approved_origins: list[str] = Field(default_factory=list)
    # The page-derived origin currently awaiting the user's yes/no, set when the
    # plan pauses on an origin-approval question and cleared when it is answered
    # (answer() reads it to decide whether to approve). SERIALIZED for the same
    # reason as approved_origins. Empty = no origin-approval is pending.
    pending_origin_approval: Optional[str] = None
    # The exact URL the paused browse wanted to open on that origin (2026-07-19,
    # the WWR resume-blind incident: after the user's "yes" the revised browse
    # restarted at the ORIGINAL start_url, wandered the homepage and hit the
    # stuck-limit — the approved destination itself had been thrown away). On an
    # affirmative answer, code stamps this into the paused browse step's
    # start_url so the resumed run opens the page the user just approved.
    # SERIALIZED beside pending_origin_approval; defaulted for old payloads.
    pending_origin_url: Optional[str] = None
    # The address the user NAMED that turned out not to exist (2026-08-01) —
    # set when a browse pauses on "did you mean…?" and cleared when answered —
    # and the verified alternatives that pause offered. Both SERIALIZED beside
    # the other pending_* markers (the pause PARKS the plan, so an excluded
    # field would lose the question's own options across the round trip and the
    # answer could not be matched against anything). Defaulted for old payloads.
    pending_site_correction: Optional[str] = None
    pending_site_candidates: list[str] = Field(default_factory=list)
    # How many "did you mean…?" pauses this plan has spent (cap
    # _MAX_SITE_CORRECTIONS). SERIALIZED for the browse_handoffs reason: the ask
    # parks the plan, so a counter that did not survive the park would let a
    # correction chain restart from zero on every resume and never terminate.
    site_corrections: int = 0
    # Every address this plan has learned is WRONG, mapped to the one the user
    # confirmed instead (2026-08-02). _apply_site_correction re-points the steps
    # that are pending WHEN IT RUNS, but it never touches plan.goal — and
    # ground_origins reads the goal, so the dead host stays grounded forever and
    # a later revise (which drops and re-drafts pending steps) can quietly aim a
    # new step straight back at it. This is the record that lets code re-point
    # those too, the _inject_approved_origins pattern. SERIALIZED: the pause
    # parks the plan, and a correction forgotten across the park is the bug.
    site_corrections_applied: dict[str, str] = Field(default_factory=dict)
    # A world-acting gesture (send / post / submit / upload / like / follow /
    # delete / buy…) a READ browse loop reached and STOPPED at (2026-07-22): an
    # action on a live site is NEVER performed without the user's yes. Holds the
    # human-readable description the pause asks about ("send the message 'hi'…"),
    # set when the plan pauses on the action-approval question and cleared when it
    # is answered. SERIALIZED beside the other pending_* markers; defaulted for
    # old payloads.
    pending_action_approval: Optional[str] = None
    # The FINGERPRINT of the gesture being asked about (browser.loop's
    # gesture_fingerprint: kind + the control's role/name/href + the host), set
    # beside pending_action_approval and cleared with it.
    pending_action_fingerprint: Optional[str] = None
    # The gesture an affirmative answer approved, injected into the resumed browse
    # step's parameters so the loop may perform THAT ONE action.
    #
    # THIS WAS A BOOLEAN until 2026-07-26, and that was the weakest link in the
    # browser safety model: `action_approved=True` lifted the READ-mode gesture
    # gate for EVERY world-acting gesture in the resumed run, so a yes to "send
    # this message" also authorised any buy, delete or post the loop chose next.
    # Everything around it is bound to a fingerprint and consumed once
    # (session.arm_commit / _commit_fingerprint); this now is too. A different
    # control, a different gesture, or a different site pauses again.
    #
    # SERIALIZED; defaulted, so a plan parked before this change deserializes with
    # NO permit — it pauses again rather than inheriting a blanket yes, which is
    # the safe direction.
    approved_action_fingerprint: str = ""
    # How many times this plan has handed a browse CAPTCHA / verification
    # challenge off to the user (2026-07-19). Some challenges (Cloudflare
    # Turnstile) fingerprint the automated browser and RE-ISSUE no matter how
    # many times the user solves the checkbox by hand — so pausing again would
    # trap the user in an unwinnable loop (live report). This counter, checked in
    # _execute_node's challenge branch, caps the retries: past the limit the plan
    # STOPS honestly ("I couldn't get past it") instead of pausing forever.
    # SERIALIZED so it survives the park/resume across each hand-off; defaulted so
    # plans parked before this field deserialize.
    challenge_attempts: int = 0
    # True once this plan is known to require ACTING on a live website — its
    # goal is a browse-submit goal (apply/sign-in/checkout/… on a named site) or
    # some accepted draft used browse/browse_commit (2026-07-19). It only ever
    # flips False→True and never back, so once a task is a browser task a later
    # replan can never quietly downgrade it to a read-only web fetch
    # (read_webpage/browse_page/web_search) — which 403s or cannot act on the
    # very sites browse exists for. Consulted by _browse_downgrade_violation.
    # SERIALIZED so it survives park/resume across an approval pause; defaulted
    # so plans parked before this field deserialize.
    is_browse_task: bool = False
    # The form field a commit-mode browse paused on because its value was in
    # neither the autofill profile nor the user's words (15.2, field-learning
    # 2026-07-19). Its RAW name (e.g. "ctl00$ContentPlaceHolder1$txtEmail") is
    # kept so answer() can, when the user supplies the value, DERIVE a clean
    # profile key from it (autofill.derive_field_identity) and SAVE the answer to
    # the autofill profile — so the same field never has to be asked again.
    # SERIALIZED beside the other pending_* markers; defaulted for old payloads.
    pending_fill_field: Optional[str] = None
    # An OPTIONAL sign-in / sign-up offer the current commit page showed (the
    # soft-auth hand-off, 2026-07-19). Unlike a hard login wall this does not
    # block the form — the site merely OFFERS an account — so the user chooses:
    # sign in, sign up, or apply as a guest. Set when the plan pauses on the
    # auth-offer question; the site host is stored so answer() can open a sign-in
    # window for it. SERIALIZED; defaulted for old payloads.
    pending_auth_offer: Optional[str] = None
    # The exact page URL the auth offer was seen on — recorded so that whatever
    # the user chooses, that page is added to auth_resolved_urls and never
    # re-asks. SERIALIZED beside pending_auth_offer; defaulted.
    pending_auth_url: Optional[str] = None
    # "Which one did you mean?" — several things on the page matched the user's
    # words EQUALLY well (2026-08-02, browser/choice.py). `pending_target_kind`
    # is "item" (a product on a listing) or "option" (a value in a size/colour
    # control); `pending_target_field` names the control for an option ask; the
    # OPTIONS are kept because the pause PARKS the plan and an excluded list
    # would leave the answer with nothing to be matched against (the
    # pending_site_candidates lesson). All SERIALIZED; defaulted for old payloads.
    pending_target_choice: Optional[str] = None
    pending_target_kind: str = ""
    pending_target_field: str = ""
    pending_target_options: list[str] = Field(default_factory=list)
    # What the user picked, per kind — replayed onto every browse step by
    # _inject_target_choices so a later revise round (which re-drafts pending
    # steps from the goal, and the goal is still the ambiguous sentence) cannot
    # quietly lose the answer. ENFORCE, NEVER TRUST — the _inject_site_corrections
    # pattern. SERIALIZED; defaulted.
    chosen_target: str = ""
    chosen_option: str = ""
    # How many "which one did you mean?" pauses this plan has spent. Its own
    # small budget on top of browse_handoffs, for the _MAX_SITE_CORRECTIONS
    # reason: a third round means the answers are not narrowing anything, and
    # chaining guesses off guesses is how a pause loop starts. SERIALIZED — the
    # ask parks the plan, so a counter that did not survive would restart at zero
    # on every resume and never terminate.
    target_choices: int = 0
    # STRUCTURAL browse hand-offs made so far (missing form value / optional
    # sign-in offer / off-site origin approval) — counted SEPARATELY from
    # questions_asked because a real application legitimately needs many, and the
    # MAX_QUESTIONS=3 clarification cap would fail the flow at the third field
    # (2026-07-19). Bounded by _MAX_BROWSE_HANDOFFS. SERIALIZED so it survives
    # each park/resume; defaulted for old payloads.
    browse_handoffs: int = 0
    # STRUCTURAL same-named-folder hand-offs made so far ("there are 2 folders
    # named 'Downloads' — which one?"). Counted SEPARATELY from questions_asked
    # for the browse_handoffs reason turned around: a MUTATING step must never
    # lose its turn to three LLM clarifications and then move 85 files into the
    # wrong drive, which is exactly what happened on 2026-08-01 (there, because
    # the guard never ran at all). Bounded by _MAX_FOLDER_HANDOFFS. SERIALIZED
    # — the ask PARKS the plan, so the counter has to survive the round trip;
    # defaulted for old payloads.
    folder_handoffs: int = 0
    # Pages (by URL) on which the user has already decided the sign-in offer —
    # so a commit browse asks AT MOST once per distinct page (the "every time it
    # sees one" setting means every distinct page, not every observation, or the
    # loop would re-ask the same page forever after "apply as guest"). Carried
    # into the browse loop so it skips the auth-offer detection for these URLs.
    # SERIALIZED (rides the parked payload across each pause); defaulted.
    auth_resolved_urls: list[str] = Field(default_factory=list)
    # RESTART HONESTY: a browse pause can promise "the window stays open and
    # I'll carry on there", but held sessions are memory-only — after a backend
    # restart the resume relaunches from the start. When that happens this note
    # is set and PREFIXES the next hand-off question, so the promise-break is
    # said out loud instead of silently re-driving the form. SERIALIZED (the
    # broken promise is only discoverable ON the resume, which may itself be
    # after a park); defaulted for old payloads.
    browse_note: str = ""
    # A hard LOGIN wall the current browse hit, awaiting the user's choice
    # (2026-07-23). Unlike a soft auth-offer this page BLOCKED the loop — but many
    # sites (anikoto &c.) are usable as a guest and a modal/overlay can read as a
    # wall, so the pause offers "continue without signing in" alongside the
    # sign-in hand-off. Set to the wall's site when the plan pauses on the
    # login-wall question so answer() can tell that reply apart. SERIALIZED beside
    # the other pending_* markers; defaulted for old payloads.
    pending_login_wall: Optional[str] = None
    # Set True when the user answered a login-wall pause with "continue without
    # signing in" (2026-07-23), and injected into the resumed browse step's
    # parameters so the loop does NOT stop on a login wall for that run. Only ever
    # True on such a resume; a later replan re-drafts the step and this clears.
    # SERIALIZED; defaulted for old payloads.
    skip_login_wall: bool = False

    def next_pending_index(self) -> Optional[int]:
        for i, step in enumerate(self.steps):
            if step.status == StepStatus.PENDING:
                return i
        return None

    def pending_steps(self) -> list[PlanStep]:
        return [s for s in self.steps if s.status == StepStatus.PENDING]

    def contract_hash(self) -> str:
        """Stable identity of everything this plan is asking permission FOR.

        ⚠️ WHY THIS EXISTS (2026-08-03, Tier 2 item 8). Consent to a write is
        consent to a SIGNATURE SET — the exact commands and paths on the card —
        and that is precisely why `task_router._is_typed_approval` REFUSES a
        typed "yes" and nudges the user back to the button: a bare word is not
        bound to anything.

        An off-card approval (spoken, or from a phone) is only honest if it
        carries the same binding. So the client echoes back the hash of the
        contract it was GIVEN, and the server RE-DERIVES this from the plan it
        just popped. A client can only hold the hash if it received the
        contract; a plan whose steps changed since produces a different hash and
        the approval is refused. The card's guarantee, delivered through a
        different sense — not a weakening of it.

        Built from `signature()` so it inherits exactly what approval already
        means, and ORDERED so a reshuffle is a different contract."""
        import hashlib

        payload = json.dumps([s.signature() for s in self.pending_steps()])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def completed_steps(self) -> list[PlanStep]:
        return [s for s in self.steps if s.status == StepStatus.COMPLETED]

    @property
    def requires_approval(self) -> bool:
        return any(s.requires_approval for s in self.pending_steps())


# ================================================================ LLM output

class PlannedStepDraft(BaseModel):
    """A step exactly as the planner LLM emitted it — untrusted."""
    model_config = ConfigDict(extra="ignore")

    description: str = ""
    tool: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("description", "tool", mode="before")
    @classmethod
    def _strs(cls, v: Any) -> str:
        return str(v or "").strip()

    @field_validator("parameters", mode="before")
    @classmethod
    def _params(cls, v: Any) -> dict:
        return v if isinstance(v, dict) else {}


class QuestionDraft(BaseModel):
    """A clarifying question exactly as the planner LLM emitted it — untrusted.
    Text and options are length- and count-capped in validation."""
    model_config = ConfigDict(extra="ignore")

    text: str = ""
    options: list[str] = Field(default_factory=list)

    @field_validator("text", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        return str(v or "").strip()[:500]

    @field_validator("options", mode="before")
    @classmethod
    def _options(cls, v: Any) -> list:
        if not isinstance(v, list):
            return []
        cleaned = [str(o).strip()[:300] for o in v if str(o or "").strip()]
        return cleaned[:12]


class PlanDraft(BaseModel):
    """Top-level shape of the planner LLM's JSON output — untrusted."""
    model_config = ConfigDict(extra="ignore")

    steps: list[PlannedStepDraft] = Field(default_factory=list)
    unachievable_reason: Optional[str] = None
    question: Optional[QuestionDraft] = None
    # Empty-revision disambiguation (2026-07-21): an empty `steps` on a FAILURE
    # replan means one of TWO opposite things — "the executed results already
    # accomplish the goal, nothing more is needed" or "the rest is impossible" —
    # and prose in `unachievable_reason` cannot be told apart in code (the live
    # incident: a plan FAILED carrying the message "The goal has been fully
    # accomplished"). This flag is the structural comparator: True = the empty
    # revision is a COMPLETION, not a surrender. Backstopped in the planner —
    # it is only honored when at least one step actually completed.
    goal_accomplished: bool = False

    @field_validator("steps", mode="before")
    @classmethod
    def _steps(cls, v: Any) -> list:
        return v if isinstance(v, list) else []

    @field_validator("goal_accomplished", mode="before")
    @classmethod
    def _accomplished(cls, v: Any) -> bool:
        if isinstance(v, bool):
            return v
        return str(v or "").strip().lower() == "true"

    @field_validator("unachievable_reason", mode="before")
    @classmethod
    def _reason(cls, v: Any) -> Optional[str]:
        text = str(v or "").strip()
        return text if text and text.lower() != "null" else None

    @field_validator("question", mode="before")
    @classmethod
    def _question(cls, v: Any) -> Optional[dict]:
        return v if isinstance(v, dict) else None
