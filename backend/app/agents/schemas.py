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
    the credentials). Empty = a plain clarifying question."""

    text: str
    options: list[str] = Field(default_factory=list)
    kind: str = ""


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
    # STRUCTURAL browse hand-offs made so far (missing form value / optional
    # sign-in offer / off-site origin approval) — counted SEPARATELY from
    # questions_asked because a real application legitimately needs many, and the
    # MAX_QUESTIONS=3 clarification cap would fail the flow at the third field
    # (2026-07-19). Bounded by _MAX_BROWSE_HANDOFFS. SERIALIZED so it survives
    # each park/resume; defaulted for old payloads.
    browse_handoffs: int = 0
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

    def next_pending_index(self) -> Optional[int]:
        for i, step in enumerate(self.steps):
            if step.status == StepStatus.PENDING:
                return i
        return None

    def pending_steps(self) -> list[PlanStep]:
        return [s for s in self.steps if s.status == StepStatus.PENDING]

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

    @field_validator("steps", mode="before")
    @classmethod
    def _steps(cls, v: Any) -> list:
        return v if isinstance(v, list) else []

    @field_validator("unachievable_reason", mode="before")
    @classmethod
    def _reason(cls, v: Any) -> Optional[str]:
        text = str(v or "").strip()
        return text if text and text.lower() != "null" else None

    @field_validator("question", mode="before")
    @classmethod
    def _question(cls, v: Any) -> Optional[dict]:
        return v if isinstance(v, dict) else None
