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
    and any write/destructive step still pauses for approval afterwards."""

    text: str
    options: list[str] = Field(default_factory=list)


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
