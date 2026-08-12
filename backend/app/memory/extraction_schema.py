"""
Furi OS — Extraction Output Schema
Pydantic validation for the LLM entity-extraction JSON.

The extractor LLM returns free-form JSON; this module turns it into a typed,
clamped, default-filled ExtractionResult so the pipeline never has to do
scattered .get()/isinstance checks. Unknown keys are ignored, null values
coerce to safe defaults, confidences are clamped to [0, 1].
"""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.memory.contact_validation import normalize_birthday, normalize_email


def _none_to_default(value, default):
    return default if value is None else value


class NewFact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fact: str = ""
    category: str = "other"

    @field_validator("fact", "category", mode="before")
    @classmethod
    def _no_none(cls, v):
        return _none_to_default(v, "")


class PersonMentioned(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    email: Optional[str] = None
    phone: Optional[str] = None
    relationship: Optional[str] = None
    skills: list[str] = Field(default_factory=list)
    birthday: Optional[str] = None
    important_dates: Optional[dict] = None
    new_facts: list[NewFact] = Field(default_factory=list)

    @field_validator("name", mode="before")
    @classmethod
    def _name_str(cls, v):
        return str(_none_to_default(v, "")).strip()

    @field_validator("skills", "new_facts", mode="before")
    @classmethod
    def _lists(cls, v):
        return _none_to_default(v, [])

    # Deterministic net (contact_validation.py): a hallucinated address or an
    # impossible date becomes None here — it never reaches store_contact, so
    # it never parks in a PendingResolution or triggers a create question.
    @field_validator("email", mode="before")
    @classmethod
    def _email(cls, v):
        return normalize_email(v)

    @field_validator("birthday", mode="before")
    @classmethod
    def _birthday(cls, v):
        return normalize_birthday(v)


class RelationshipEdge(BaseModel):
    model_config = ConfigDict(extra="ignore")

    source_name: str = ""
    source_type: str = "contact"
    edge_type: str = ""
    target_name: str = ""
    target_type: str = "contact"
    confidence: float = 0.7
    edge_label: Optional[str] = None

    @field_validator("source_name", "target_name", "edge_type", mode="before")
    @classmethod
    def _strs(cls, v):
        return str(_none_to_default(v, "")).strip()

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, v):
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.7


class UserProfileEnrichment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: Optional[str] = None
    profession: Optional[str] = None
    location: Optional[str] = None
    background: Optional[str] = None
    work_style: Optional[str] = None
    skills: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)

    @field_validator("name", "profession", "location", "background", "work_style", mode="before")
    @classmethod
    def _strip_null_strings(cls, v):
        # The LLM sometimes echoes the literal placeholder text back
        if v in (None, "null", "null or string", ""):
            return None
        return str(v)

    @field_validator("skills", "languages", mode="before")
    @classmethod
    def _lists(cls, v):
        return _none_to_default(v, [])

    def non_empty_fields(self) -> dict:
        """Only the fields that actually carry new information."""
        out = {}
        for f in ("name", "profession", "location", "background", "work_style"):
            val = getattr(self, f)
            if val:
                out[f] = val
        for f in ("skills", "languages"):
            val = getattr(self, f)
            if val:
                out[f] = val
        return out


class SharedFact(BaseModel):
    """
    A fact about the user, optionally shared with contacts.

    Perspectives carry placeholders that Python substitutes AFTER identity
    resolution:
      {USER}            → the user's real name (from UserProfile)
      {CONTACT:<name>}  → the resolved full contact name for <name>-as-said
    """
    model_config = ConfigDict(extra="ignore")

    fact_user_perspective: str = ""
    fact_contact_perspective: str = ""
    subject: Literal["user", "shared"] = "user"
    related_contacts: list[str] = Field(default_factory=list)
    event_date: Optional[str] = None  # YYYY-MM-DD

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_shapes(cls, data):
        # Legacy: a plain string fact, or an object with a single "fact" key
        if isinstance(data, str):
            return {"fact_user_perspective": data, "subject": "user"}
        if isinstance(data, dict) and "fact" in data and "fact_user_perspective" not in data:
            data = dict(data)
            data["fact_user_perspective"] = data.pop("fact") or ""
        return data

    @field_validator("fact_user_perspective", "fact_contact_perspective", mode="before")
    @classmethod
    def _strs(cls, v):
        return str(_none_to_default(v, "")).strip()

    @field_validator("subject", mode="before")
    @classmethod
    def _subject(cls, v):
        return v if v in ("user", "shared") else "user"

    @field_validator("related_contacts", mode="before")
    @classmethod
    def _contacts(cls, v):
        v = _none_to_default(v, [])
        return [str(c).strip() for c in v if c and str(c).strip()]

    @field_validator("event_date", mode="before")
    @classmethod
    def _date(cls, v):
        if not v or v == "null":
            return None
        return str(v).strip()


class ImportantEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = "Notable Event"
    summary: str = ""
    importance: float = 0.7

    @field_validator("title", "summary", mode="before")
    @classmethod
    def _strs(cls, v):
        return str(_none_to_default(v, "")).strip()

    @field_validator("importance", mode="before")
    @classmethod
    def _clamp(cls, v):
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.7


class PreferenceItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str = ""
    value: str = ""
    evidence: Optional[str] = None

    @field_validator("key", "value", mode="before")
    @classmethod
    def _strs(cls, v):
        return str(_none_to_default(v, "")).strip()


class OpenThread(BaseModel):
    """An ongoing concern / goal the user has an open stake in and would
    appreciate a follow-up on (Phase 11.3). Deliberately narrow — only genuine
    pending matters ("worried about the deadline", "waiting to hear back on the
    interview"), never a completed fact or a passing remark."""
    model_config = ConfigDict(extra="ignore")

    title: str = ""
    description: Optional[str] = None
    event_date: Optional[str] = None  # YYYY-MM-DD when a relevant date is known

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, v):
        return str(_none_to_default(v, "")).strip()

    @field_validator("description", "event_date", mode="before")
    @classmethod
    def _opt(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s or None


class ExtractionResult(BaseModel):
    """Validated top-level shape of the extractor LLM's JSON output."""
    model_config = ConfigDict(extra="ignore")

    people_mentioned: list[PersonMentioned] = Field(default_factory=list)
    contacts_to_delete: list[str] = Field(default_factory=list)
    relationships: list[RelationshipEdge] = Field(default_factory=list)
    user_profile_enrichment: Optional[UserProfileEnrichment] = None
    facts_about_user: list[SharedFact] = Field(default_factory=list)
    facts_to_supersede: list[str] = Field(default_factory=list)
    important_events: list[ImportantEvent] = Field(default_factory=list)
    preferences: list[PreferenceItem] = Field(default_factory=list)
    open_threads: list[OpenThread] = Field(default_factory=list)

    @field_validator(
        "people_mentioned", "contacts_to_delete", "relationships",
        "facts_about_user", "facts_to_supersede", "important_events",
        "preferences", "open_threads",
        mode="before",
    )
    @classmethod
    def _lists(cls, v):
        return _none_to_default(v, [])

    @field_validator("contacts_to_delete", "facts_to_supersede", mode="after")
    @classmethod
    def _clean_str_lists(cls, v):
        return [s.strip() for s in v if s and s.strip()]
