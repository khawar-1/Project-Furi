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
