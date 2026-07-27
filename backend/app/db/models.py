"""
Jarvis OS — ORM Models
All SQLite tables for the memory engine, contacts, episodes, preferences, messages,
UserProfile (living identity wiki), and EntityEdge (typed relationship graph).
"""
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    """Timezone-correct UTC timestamp, stored naive for SQLite consistency."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_iso(dt: datetime | None) -> str | None:
    """Serialize a stored naive-UTC datetime with an explicit UTC offset.

    The DB convention is naive UTC (utc_now above). A bare .isoformat() of
    such a value has no timezone marker, so the frontend's `new Date(iso)`
    reads it as LOCAL time — every displayed timestamp shifts by the user's
    UTC offset. API serializers must use this instead of .isoformat()."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# Kept as the column default so existing call sites stay unchanged
_now = utc_now


# ============================================================
# Chat Messages
# ============================================================
class Message(Base):
    """Stores all conversation messages for history and context."""
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    role: Mapped[str] = mapped_column(String(16))  # "user" | "assistant" | "system"
    content: Mapped[str] = mapped_column(Text)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tokens_used: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    # Phase 6 Part 4 — set once this message's content has been embedded into
    # the "conversation_messages" Qdrant collection. NULL = not yet indexed; it
    # is the incremental cursor the backfill pass reads (mirrors FileIndex's
    # size/mtime skip). Never leaves the machine — fastembed is local.
    embedded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# ============================================================
# Semantic Memory — Facts about the user
# ============================================================
class SemanticMemory(Base):
    """
    Stores discrete facts. Subject distinguishes whose fact it is:
      - 'user'    — purely about the user (shown in About Me tab)
      - 'contact' — purely about a contact (stored in contact's fact log only)
      - 'shared'  — involves user + one or more contacts (shown in both)
    """
    __tablename__ = "semantic_memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # work | personal | preference | skill | fact
    source: Mapped[str] = mapped_column(String(32), default="inferred")  # inferred | explicit | extracted
    subject: Mapped[str] = mapped_column(String(16), default="user")  # user | contact | shared
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    event_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)  # when the fact's event happened (not when it was recorded)
    qdrant_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)  # Vector DB reference
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    contact_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("contacts.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# ============================================================
# Relationship Memory — People the user knows
# ============================================================
class Contact(Base):
    """
    Stores people in the user's network with relationship context.
    Powers identity resolution when the user says "Email Ahmed".
    """
    __tablename__ = "contacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(256), nullable=True, index=True)
    phone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    organization: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    relationship_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # client | friend | recruiter | colleague | family
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    skills: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON array: ["Python", "AI", "React"]
    birthday: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Phase 5 Part 4: the pending scheduled_jobs row for this contact's next
    # birthday reminder (mirrors Reminder.job_id). Internal plumbing — not
    # serialized to the API. sync_contact_birthday_job keeps it current.
    birthday_job_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)  # scheduled_jobs.id
    important_dates: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON dictionary or list
    last_interaction: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    interaction_count: Mapped[int] = mapped_column(Integer, default=0)
    qdrant_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    interactions: Mapped[list["ContactInteraction"]] = relationship(
        "ContactInteraction",
        back_populates="contact",
        order_by="ContactInteraction.interaction_date.desc()",
        cascade="all, delete-orphan",  # deleting a contact removes their fact log
    )


# ============================================================
# Contact Interactions — Per-contact interaction history
# ============================================================
class ContactInteraction(Base):
    """
    Records individual interactions with a contact.
    Used to build a timeline on the contact detail view.
    """
    __tablename__ = "contact_interactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    contact_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("contacts.id"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), default="other", server_default="other")
    event_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)  # when the event happened (not when it was recorded)
    interaction_date: Mapped[datetime] = mapped_column(DateTime, default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    contact: Mapped["Contact"] = relationship("Contact", back_populates="interactions")


# ============================================================
# Episodic Memory — Important events and conversations
# ============================================================
class Episode(Base):
    """
    Stores significant events: completed tasks, key conversations, milestones.
    """
    __tablename__ = "episodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    episode_type: Mapped[str] = mapped_column(String(64), default="conversation")  # conversation | task | event | milestone
    related_session_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    related_contact_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("contacts.id"), nullable=True)
    qdrant_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    related_contact: Mapped[Optional["Contact"]] = relationship("Contact", foreign_keys=[related_contact_id])


# ============================================================
# Preference Memory — Auto-extracted user preferences
# ============================================================
class Preference(Base):
    """
    Automatically extracted preferences from user behavior.
    Example: "User prefers short, concise emails" (extracted from repeated edits).
    """
    __tablename__ = "preferences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)  # e.g. "email_style"
    value: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. "concise and professional"
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="inferred")  # inferred | explicit
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# ============================================================
# User Profile — Single-row living identity wiki (Phase 2.5)
# ============================================================
class UserProfile(Base):
    """
    A single-row living document about the user.
    Stores stable identity facts: name, profession, location, skills.
    Gets merged/updated as the user reveals more about themselves.
    Fluid/contextual facts belong in semantic_memories instead.
    """
    __tablename__ = "user_profile"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Core identity fields
    name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    profession: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    location: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    # JSON arrays stored as text
    skills: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # ["Python", "React"]
    languages: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # ["English", "Urdu"]
    # Free-text fields
    work_style: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    background: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # e.g. "recently graduated"
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# ============================================================
# Entity Edges — Typed relationship graph (Phase 2.5)
# ============================================================
EDGE_TYPES = {
    "WORKS_ON",           # person works on a project
    "COLLABORATES_WITH",  # person works with another person
    "FRIEND_OF",          # personal relationship
    "CLIENT_OF",          # business relationship
    "OTHER",              # catch-all with free-text label
}

class EntityEdge(Base):
    """
    Stores typed directed relationships between entities.
    Enables multi-hop traversal: 'Who can help with X project?'
    → Find project → Find WORKS_ON contacts → Find HAS_SKILL contacts.
    """
    __tablename__ = "entity_edges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Source entity (person, project, skill)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)   # "contact" | "project" | "skill"
    source_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)  # FK to contacts/projects
    source_name: Mapped[str] = mapped_column(String(256), nullable=False)  # denormalized for fast display
    # Edge type
    edge_type: Mapped[str] = mapped_column(String(64), nullable=False)     # one of EDGE_TYPES
    edge_label: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)  # free-text for OTHER type
    # Target entity
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)   # "contact" | "project" | "skill"
    target_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    target_name: Mapped[str] = mapped_column(String(256), nullable=False)  # denormalized
    # Confidence: 0.7 for inferred, 1.0 for explicit
    confidence: Mapped[float] = mapped_column(Float, default=0.7)
    source_session: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)  # session it was extracted from
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# ============================================================
# Parked Plans — Persisted plans awaiting approval / an answer (Phase 3.5)
# ============================================================
class ParkedPlan(Base):
    """
    A plan paused for user approval or a clarifying-question answer,
    persisted so a backend restart (or the in-memory cache TTL) never
    silently destroys it. The in-memory plan_store stays the hot cache;
    this table is the truth. Rows are deleted when the plan is consumed
    (approved / cancelled / answered) or when expires_at passes.
    """
    __tablename__ = "parked_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # AgentPlan.id
    session_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32))  # awaiting_approval | awaiting_choice
    payload: Mapped[str] = mapped_column(Text)  # full AgentPlan JSON incl. planner inputs
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


# ============================================================
# Pending Session State — Persisted parked memory questions (Phase 3.5)
# ============================================================
class PendingSessionState(Base):
    """
    Snapshot of a ConversationSession's parked questions ("which jamil?" /
    "add daud?") plus the names the user already confirmed. Restored when the
    in-memory session is gone (restart) or TTL-evicted, so an unanswered
    question survives a lunch break. The in-memory session stays authoritative
    while it is alive; this row only resurrects cold sessions.
    """
    __tablename__ = "pending_resolutions"

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    resolution: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # PendingResolution JSON
    creation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # PendingCreation JSON
    confirmed_names: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # {as-said: contact_id}
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


# ============================================================
# Reminders — User-facing timed nudges (Phase 4, Part 4)
# ============================================================
class Reminder(Base):
    """
    A reminder the user asked for ("remind me at 6 to call Jamil"). This is
    the user-facing record (text, due time, which chat session it belongs
    to); the scheduled_jobs row it points at (job_id) is the actual timer.
    The fire-vs-cancel race is settled once, at the scheduled_jobs level
    (JarvisScheduler's own atomic claim) — this row's status is set to
    mirror that outcome, not to re-arbitrate it.
    session_id is the chat session that created it — a fired reminder's
    message is persisted into that session's history so it's visible even
    if no window was open to catch the push event.
    """
    __tablename__ = "reminders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)  # naive UTC
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending | fired | cancelled
    job_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)  # scheduled_jobs.id
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    fired_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# ============================================================
# Background Tasks — Plans that escape the chat turn (Phase 4, Part 5)
# ============================================================
class Task(Base):
    """
    A user goal executing in the background, wrapping an AgentPlan. SQLite is
    the truth for task state: the asyncio task running the plan is only the
    engine, and a backend restart marks still-`running` rows failed at
    startup (fail_interrupted_tasks) — steps may have run; ActivityLog is
    the audit trail. A task paused for approval/answer survives a restart
    through its parked_plans row (that table stays the resume truth);
    plan_payload here is a display/audit snapshot, never resumed from.
    """
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    # running | awaiting_approval | awaiting_choice | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(24), default="running", index=True)
    # The domain agent that owns this task (the boss+agents model): file /
    # email / calendar / research / browser / general. Used by the Agents panel,
    # progress queries, and telemetry. Nullable for rows created before the
    # column existed.
    domain: Mapped[Optional[str]] = mapped_column(String(24), nullable=True, index=True)
    plan_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    plan_payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # AgentPlan JSON snapshot
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # final user-facing outcome
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# ============================================================
# Routines — Teachable procedural memory (Phase 6, Part 5)
# ============================================================
class Routine(Base):
    """
    A named, repeatable procedure the user taught once ("save this as a
    routine called 'clean desktop'") and later invokes by name ("run my clean
    desktop routine"). We store the GOAL STRING (goal_template), never a frozen
    plan: every run is replanned fresh through the agent planner, so the
    structural approval gate, path guards, and recipient/event-id locks all
    re-apply automatically — a routine can never smuggle a pre-approved
    destructive plan past the gate.

    normalized_name is the lookup key (lowercased, punctuation-stripped) so
    "Clean Desktop", "clean desktop!", and "clean  desktop" all resolve to the
    same routine; name keeps the user's own casing for display. Teaching an
    existing name replaces its goal_template (re-teach, not duplicate). The ONE
    accessor is app/core/routines.py — the router never touches this table.
    """
    __tablename__ = "routines"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(256), nullable=False)  # display, user's casing
    normalized_name: Mapped[str] = mapped_column(String(256), nullable=False, unique=True, index=True)
    goal_template: Mapped[str] = mapped_column(Text, nullable=False)  # raw goal re-fed to the planner
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # --- Optional time trigger (Phase 10, Part 2 — scheduled routines) ---
    # schedule_type None = manual-only (invoke by name). "interval" | "daily" |
    # "weekly" auto-run the goal_template through start_task at the next
    # occurrence (the plan is re-derived, so the approval gate re-applies — a
    # scheduled write pauses for approval, a read-only routine completes).
    schedule_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    schedule_minute: Mapped[int] = mapped_column(Integer, default=0)      # 0-59
    schedule_hour: Mapped[int] = mapped_column(Integer, default=9)        # 0-23 (daily/weekly)
    schedule_weekday: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 0=Mon..6=Sun (weekly)
    schedule_interval_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # interval
    # The scheduled_jobs.id pointer for this routine's next run (per-row, the
    # Contact.birthday_job_id template). Internal plumbing — never serialized.
    schedule_job_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# ============================================================
# Initiative suggestions — proactive nudges (Phase 9)
# ============================================================
class Suggestion(Base):
    """
    A proactive suggestion Jarvis volunteered from the initiative heartbeat —
    the user-facing record of the ambient suggestion feed. This is Jarvis's
    OWN generated content (a nudge, a question, or an action it chose to
    surface), so it persists to SQLite (unlike Phase 8 sensed data, which is
    retention=none): the feed must survive a restart and stay accept/dismiss-
    able. The ONE accessor is app/core/suggestions.py — the router/handler
    never touch this table directly (the reminders-router rule).

    `autonomy` records what the code-owned policy classified this candidate as:
    - "suggest" — informational only (no goal, nothing to run).
    - "ask"     — carries a `goal`; accepting starts an approval-gated Task.
    - "acted"   — the heartbeat already started the Task (only under autonomy
                  level "act"); every write inside still paused at the gate.
    `goal` is a GOAL STRING re-fed to the planner on accept/act, never a frozen
    plan — so the approval gate + path/recipient/event-id locks re-apply (the
    Routine.goal_template principle). `dedupe_key` is a normalized signature
    that blocks re-surfacing the same nudge within a cooldown window.
    """
    __tablename__ = "suggestions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(64), default="general", index=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, default="")  # the "why it matters"
    autonomy: Mapped[str] = mapped_column(String(16), default="suggest")  # suggest | ask | act
    priority: Mapped[str] = mapped_column(String(16), default="normal")  # low | normal | high
    goal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # goal string for ask/act
    # pending | accepted | dismissed | acted | expired
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    task_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)  # set when a Task starts
    dedupe_key: Mapped[str] = mapped_column(String(128), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)


# ============================================================
# Goal threads — ongoing concerns Jarvis nudges toward (Phase 11, Part 3)
# ============================================================
class GoalThread(Base):
    """A lightweight ongoing-concern / goal thread ("the deadline I was worried
    about", "prepping for the interview") that Jarvis can proactively follow up
    on — "last week you were worried about the deadline; did it land?".

    Captured from conversation by the extractor (source="extractor") or created
    manually. `status` tracks the lifecycle (open → resolved | dropped);
    `next_check_at` is when a nudge becomes due, `last_nudged_at` throttles
    repeat nudges. `normalized_title` is a dedupe key so re-mentioning the same
    concern updates the open thread instead of piling up duplicates. The ONE
    accessor is app/core/goal_threads.py — the initiative gatherer/router never
    touch this table directly (the reminders rule).
    """
    __tablename__ = "goal_threads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_title: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # open | resolved | dropped
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    contact_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("contacts.id"), nullable=True, index=True
    )
    event_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)  # deadline/relevant date
    next_check_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    last_nudged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="extractor")  # extractor | manual
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# ============================================================
# Autofill Profile — curated data for browser form-filling (Phase 15.2)
# ============================================================
class AutofillField(Base):
    """One curated field of the user's autofill PROFILE — the grounded data
    source a commit-mode browse fills forms from (full name, email, phone, a
    resume path, ...). The profile is the grounding corpus for form fills
    exactly as the recipient/upload locks work: a value the loop types must
    trace to one of these rows or the user's own words, never a web page (see
    app/agents/browser_grounding.fill_value_is_grounded).

    `kind` distinguishes plain text, a link, a document (value is a file PATH,
    validated with the file-tools path safety on write), and a SECRET (value is
    sensitive — display-masked in the UI and NEVER placed in any LLM prompt or
    chat history; code substitutes it at fill time, the password-never-read
    rule). `key` is the stable lowercase identifier (unique); `label` is the
    human name shown in Settings. The ONE accessor is app/core/autofill.py (the
    reminders rule — the API router never touches this table directly)."""
    __tablename__ = "autofill_fields"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    # text | link | document | secret
    kind: Mapped[str] = mapped_column(String(16), default="text", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# ============================================================
# Scheduled Jobs — Persisted timed work (Phase 4, Part 2)
# ============================================================
class ScheduledJob(Base):
    """
    One-shot timed job for the scheduler/event bus. SQLite is the truth:
    the in-process APScheduler timers are rebuilt from pending rows at
    startup, so a restart never loses a job — one whose run_at passed
    while the backend was down fires immediately on boot (late=True).
    Settled rows (fired/failed/cancelled) are kept for inspection and
    purged after a retention window.
    """
    __tablename__ = "scheduled_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(64), index=True)  # handler registry key
    payload: Mapped[str] = mapped_column(Text, default="{}")  # JSON, handler-defined
    run_at: Mapped[datetime] = mapped_column(DateTime, index=True)  # naive UTC
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    fired_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# ============================================================
# App Settings — Generic key/value runtime configuration (Phase 5, Part 6)
# ============================================================
class AppSetting(Base):
    """
    A single runtime-configurable app setting, keyed by a dotted name with a
    JSON-encoded value. The first reusable home for settings the user toggles
    at runtime (rather than .env, which needs a restart) — Part 6's daily
    briefing config/pointer live here, and future settings can too. The ONE
    accessor is app/core/app_settings.py; call sites never touch this table
    directly (the reminders-router rule: modules own their domain).
    """
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # JSON-encoded
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# ============================================================
# Agent Activity Log — Audit trail of all tool executions
# ============================================================
class ActivityLog(Base):
    """
    Records every tool invocation by the agent for the Activity Timeline UI.
    """
    __tablename__ = "activity_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(256), nullable=False)
    parameters: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON
    result_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    permission_level: Mapped[str] = mapped_column(String(32), default="read")
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# ============================================================
# File Index — the semantic-file-search ledger (Phase 6, Part 2)
# ============================================================
class FileIndex(Base):
    """
    One row per indexed file — the incremental-reindex ledger and the
    rehydration target for a vector search. SQLite is the truth; the file's
    text lives as chunk points in the Qdrant "file_chunks" collection, each
    point carrying this row's id in its payload (file_id). A reindex skips a
    file whose size AND mtime are unchanged; a changed file's old chunks are
    deleted (deterministic ids, 0..chunk_count-1) before the new ones upsert.
    is_active=False is a soft delete (file removed, folder de-configured, or
    now excluded) — the row and its vectors are cleared, not orphaned.
    The ONE accessor is app/core/file_index.py.
    """
    __tablename__ = "file_index"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    path: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True, index=True)
    folder_root: Mapped[str] = mapped_column(String(1024), nullable=False)  # configured folder it came from
    filename: Mapped[str] = mapped_column(String(512), default="")           # basename (name search, Part 3)
    ext: Mapped[str] = mapped_column(String(32), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)                    # bytes (incremental skip)
    mtime: Mapped[float] = mapped_column(Float, default=0.0)                 # st_mtime (incremental skip)
    content_hash: Mapped[str] = mapped_column(String(64), default="")        # sha256 of extracted text
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)             # vectors written for this file
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    indexed_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
