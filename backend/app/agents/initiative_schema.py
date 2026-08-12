"""
Furi OS — Initiative candidate schema (Phase 9)

The initiative heartbeat's ONE LLM pass returns free-form JSON; this module
turns it into a typed, default-filled `InitiativeSet` so the pipeline never
does scattered .get()/isinstance checks (the extraction_schema.py discipline).
Unknown keys are ignored; null values coerce to safe defaults.

The LLM only PROPOSES `suggested_autonomy` and `priority` — the code-owned
autonomy policy (app/core/initiative.py) is the authority that decides the
final act/suggest/ask, capped by the user's configured ceiling. A candidate
with no `proposed_action` is informational (there is nothing to run).
"""
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# The vocabulary the LLM is asked to use. Anything else coerces to a safe
# default — the policy re-validates regardless, so this is only tidiness.
VALID_AUTONOMY = ("suggest", "ask", "act")
VALID_PRIORITY = ("low", "normal", "high")

# A short, curated category set keeps the feedback signal (one affinity per
# category) meaningful. An out-of-set value is kept verbatim but simply forms
# its own affinity bucket — never a crash.
SUGGESTION_CATEGORIES = (
    "calendar_prep",     # a meeting is coming up — prep / travel / agenda
    "email_followup",    # an unread/awaiting-reply thread worth surfacing
    "file_cleanup",      # tidy/organize/find files the user has been touching
    "birthday_nudge",    # a contact's birthday is near
    "task_followup",     # a recurring goal / an unfinished thread of work
    "memory_reminder",   # a noted plan/fact dated today
    "general",           # anything else
)


def _none_to(value, default):
    return default if value is None else value


class InitiativeCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = ""
    category: str = "general"
    #: The human-readable "why it matters" — surfaced verbatim in the feed card
    #: and the intelligent notification, so it must never be empty in practice
    #: (a blank one is dropped downstream).
    rationale: str = ""
    #: The one-line suggestion body shown to the user.
    body: str = ""
    #: A GOAL STRING re-fed to the planner on accept/act, or None for a pure
    #: informational nudge. Never a frozen plan — the approval gate re-applies.
    proposed_action: Optional[str] = None
    suggested_autonomy: str = "suggest"
    priority: str = "normal"

    @field_validator("title", "category", "rationale", "body", mode="before")
    @classmethod
    def _str_no_none(cls, v):
        return str(_none_to(v, "")).strip()

    @field_validator("proposed_action", mode="before")
    @classmethod
    def _action_clean(cls, v):
        if v is None:
            return None
        text = str(v).strip()
        return text or None

    @field_validator("suggested_autonomy", mode="before")
    @classmethod
    def _autonomy_clean(cls, v):
        text = str(_none_to(v, "suggest")).strip().lower()
        return text if text in VALID_AUTONOMY else "suggest"

    @field_validator("priority", mode="before")
    @classmethod
    def _priority_clean(cls, v):
        text = str(_none_to(v, "normal")).strip().lower()
        return text if text in VALID_PRIORITY else "normal"

    def is_usable(self) -> bool:
        """A candidate the pipeline can surface: it must at least say something
        (title + body) and explain itself (rationale). A blank shell from a
        half-broken generation is dropped, never surfaced."""
        return bool(self.title and self.body and self.rationale)


class InitiativeSet(BaseModel):
    """The validated top-level object the composer returns."""
    model_config = ConfigDict(extra="ignore")

    initiatives: list[InitiativeCandidate] = Field(default_factory=list)

    @field_validator("initiatives", mode="before")
    @classmethod
    def _list_no_none(cls, v):
        return v if isinstance(v, list) else []

    def usable(self) -> list[InitiativeCandidate]:
        return [c for c in self.initiatives if c.is_usable()]
