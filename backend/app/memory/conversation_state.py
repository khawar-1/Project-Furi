"""
Jarvis OS — Conversation Session
Ephemeral, session-scoped working memory. NEVER persisted to the database.

Each session gets exactly one ConversationSession. It merges what were
previously two parallel global dicts (CONVERSATION_STATES + PENDING_RESOLUTIONS)
into a single, unified per-session object.

Principle: Foreground owns state. Background owns persistence.
"""
from typing import Literal, Optional
from dataclasses import dataclass, field
import time

# Default TTL of 30 minutes per session
SESSION_TTL_SECONDS = 1800


@dataclass
class ActiveEntity:
    """
    A named entity the user is currently talking about.
    Deterministically resolved by the retriever — never LLM-inferred.
    """
    id: str
    type: Literal["contact", "project"]
    name: str
    confidence: float
    last_mentioned: float = field(default_factory=time.time)


@dataclass
class PendingResolution:
    """
    Holds an ambiguous name and its candidate matches until the user
    disambiguates. Stored here instead of a separate global dict so there
    is a single object per session.

    pending_update       — contact detail updates (email, new_facts, ...) deferred
                           until the user picks a candidate.
    pending_shared_facts — dual-perspective shared facts (dicts with
                           fact_user_perspective / fact_contact_perspective /
                           category / event_date, still containing {USER} and
                           {CONTACT:<name>} placeholders) deferred the same way.
    unresolved_mentions  — ALL ambiguous names awaiting the user's answer:
                           [{"name": as-said, "candidates": [{"id","name"}]}].
                           original_name/candidates mirror the first entry for
                           backward compatibility with single-name flows.
    resolved_so_far      — as-said name (lowercased) → contact id for names in
                           the parked facts that ARE already resolved, so a
                           multi-name fact never re-asks about them.
    """
    original_name: str
    pending_update: dict
    candidates: list
    pending_shared_facts: list = field(default_factory=list)
    unresolved_mentions: list = field(default_factory=list)
    resolved_so_far: dict = field(default_factory=dict)
    expires: float = field(default_factory=lambda: time.time() + 300)

    def mentions(self) -> list:
        """All unresolved mentions, falling back to the legacy single-name shape."""
        if self.unresolved_mentions:
            return self.unresolved_mentions
        return [{"name": self.original_name, "candidates": self.candidates}]


@dataclass
class PendingCreation:
    """
    Holds facts about a person who is NOT in contacts at all, until the user
    confirms whether to create them. The system prompt asks "X isn't in your
    contacts — want me to add them?"; the next user turn is resolved
    deterministically in Python (yes → create + apply, no → user-side only).
    """
    name: str
    pending_update: dict = field(default_factory=dict)
    pending_shared_facts: list = field(default_factory=list)
    resolved_so_far: dict = field(default_factory=dict)
    expires: float = field(default_factory=lambda: time.time() + 300)


@dataclass
class ConversationSession:
    """
    Single object representing all ephemeral, session-scoped state.

    active_entities  — all deterministically resolved entities for the current
                       conversational focus (may be 1, 2, or more, e.g. in
                       comparison mode).
    focus_entity     — the single primary entity when the conversation clearly
                       centres on one thing (e.g. "Hamil's birthday").
                       None when comparing multiple entities.
    pending_resolution — an unresolved name that needs user disambiguation.
                         Replaces the old PENDING_RESOLUTIONS global dict.
    pending_creation — an unknown person awaiting the user's yes/no on
                       whether to create them as a contact.
    current_topic    — free-text summary of what the conversation is about.
                       Useful when no structured entity has been resolved.
    last_updated     — epoch time of last activity; used for TTL eviction.
    ttl              — seconds of inactivity before this session expires.
    """
    active_entities: list[ActiveEntity] = field(default_factory=list)
    focus_entity: Optional[ActiveEntity] = None
    pending_resolution: Optional[PendingResolution] = None
    pending_creation: Optional[PendingCreation] = None
    current_topic: Optional[str] = None
    last_updated: float = field(default_factory=time.time)
    ttl: float = SESSION_TTL_SECONDS


def get_session(session_id: str) -> ConversationSession:
    """Get or create a ConversationSession for the given session_id."""
    if session_id not in CONVERSATION_SESSIONS:
        CONVERSATION_SESSIONS[session_id] = ConversationSession()
    session = CONVERSATION_SESSIONS[session_id]
    # Evict expired sessions lazily
    if time.time() - session.last_updated > session.ttl:
        CONVERSATION_SESSIONS[session_id] = ConversationSession()
    return CONVERSATION_SESSIONS[session_id]


def touch_session(session_id: str) -> None:
    """Update the last_updated timestamp of a session."""
    if session_id in CONVERSATION_SESSIONS:
        CONVERSATION_SESSIONS[session_id].last_updated = time.time()


# Global store: session_id -> ConversationSession
# One object per session. Ephemeral. NEVER persisted to database.
CONVERSATION_SESSIONS: dict[str, ConversationSession] = {}
