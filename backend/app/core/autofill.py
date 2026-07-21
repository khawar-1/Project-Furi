"""
Jarvis OS — Autofill profile (Phase 15.2)

The curated, user-owned data source a commit-mode browse fills forms from — the
grounding corpus for form fills, exactly as recipient/upload grounding works: a
value the loop types into a form must trace to this profile or the user's own
words, NEVER to a web page, never invented (app/agents/browser_grounding.
fill_value_is_grounded).

Fields are plain (name/email/phone/location/links), DOCUMENTS (a file PATH,
validated with the file-tools path safety on write — the upload_path_unsafe
rule), or SECRETS (value is sensitive: display-masked in the UI and NEVER placed
in any LLM prompt or chat history — code substitutes it at fill time by
resolving a "{{secret:<key>}}" reference, the password-never-read rule).

This is the ONE accessor for the autofill_fields table (the reminders-domain
pattern): the API router and the browser loop never touch the table directly.
The loop is handed a FillProfile — a plain, DB-free snapshot — so it can run on
the dedicated browser event loop without carrying a database session across it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AutofillField

# The recognised field kinds. `document` values are file paths (path-safety
# checked on write); `secret` values are sensitive and never leave code.
KINDS = ("text", "link", "document", "secret")

# The token the LLM types to reference a secret whose real value it never sees.
# Code substitutes the real value at fill time; the model only ever handles this
# placeholder (the password-never-read rule).
_SECRET_REF_RE = re.compile(r"\{\{\s*secret\s*:\s*([a-z0-9_\-]+)\s*\}\}", re.IGNORECASE)


def secret_ref(key: str) -> str:
    """The placeholder the model uses to reference a secret by key."""
    return "{{secret:%s}}" % normalize_key(key)


def normalize_key(key: str) -> str:
    """Stable lookup key: lowercase, trimmed, inner whitespace → underscore."""
    return re.sub(r"\s+", "_", (key or "").strip().lower())


def _basename(path: str) -> str:
    return re.split(r"[\\/]", (path or "").strip().rstrip("\\/"))[-1]


# ---------------------------------------------------- field-learning (2026-07-19)
# When a form needs a value that is in neither the profile nor the user's words,
# the loop pauses and asks; when the user answers, we FILL it AND SAVE it — so the
# same field never has to be asked twice. The intelligence is here: turn a raw
# form field name (often framework-mangled, e.g. "ctl00$ContentPlaceHolder1$
# txtEmail", "applicant[first_name]", "wpforms[fields][3]") into a stable,
# canonical profile key + a human label + the right kind. Deterministic and
# tested — no LLM, the never-guess rule; an unrecognised field falls back to a
# sanitised slug of its own name rather than being dropped.

# Common wrappers/prefixes/suffixes stripped before matching, so the meaningful
# token surfaces: ASP.NET ctl ids, input-name conventions, array brackets.
_FIELD_NOISE_RE = re.compile(
    r"(ctl\d+|contentplaceholder\d*|masterpage|placeholder|dnn_|"
    r"^txt|^inp|^fld|^frm|^edit|^input|^field|^your|"
    r"required|_?field$|_?input$|_?value$)",
    re.IGNORECASE,
)
# A URL-shaped answer, or a link-flavoured field, is stored as kind "link".
_URLISH_RE = re.compile(r"^(https?://|www\.)|\b[\w-]+\.(com|net|org|io|dev|me)\b", re.I)

# Canonical fields, matched IN ORDER (first hit wins — order matters: "first
# name" must beat the bare "name" rule). Each: (compiled pattern over the
# cleaned field text, key, label, kind).
_FIELD_RULES: list[tuple[re.Pattern, str, str, str]] = [
    (re.compile(r"first[\s_]*name|\bf[\s_]*name\b|given[\s_]*name|forename", re.I),
     "first_name", "First name", "text"),
    (re.compile(r"last[\s_]*name|\bl[\s_]*name\b|surname|family[\s_]*name", re.I),
     "last_name", "Last name", "text"),
    (re.compile(r"full[\s_]*name|your[\s_]*name|applicant[\s_]*name|\bname\b", re.I),
     "full_name", "Full name", "text"),
    (re.compile(r"e[\s\-_]*mail", re.I), "email", "Email", "text"),
    (re.compile(r"phone|mobile|\btel\b|telephone|cell", re.I),
     "phone", "Phone", "text"),
    (re.compile(r"linked[\s_]*in", re.I), "linkedin", "LinkedIn", "link"),
    (re.compile(r"git[\s_]*hub", re.I), "github", "GitHub", "link"),
    (re.compile(r"portfolio|personal[\s_]*(site|website)|\bwebsite\b|\bweb[\s_]*site\b",
                re.I), "portfolio", "Portfolio / website", "link"),
    (re.compile(r"\bcity\b|town|location|where.*based", re.I),
     "location", "Location", "text"),
    (re.compile(r"\bstate\b|province|region", re.I), "state", "State / region", "text"),
    (re.compile(r"\bcountry\b", re.I), "country", "Country", "text"),
    (re.compile(r"zip|postal|post[\s_]*code", re.I), "postcode", "Postal code", "text"),
    (re.compile(r"address|street", re.I), "address", "Address", "text"),
    (re.compile(r"company|employer|current[\s_]*(company|employer)|organi[sz]ation",
                re.I), "company", "Company", "text"),
    (re.compile(r"job[\s_]*title|\btitle\b|current[\s_]*role|position", re.I),
     "job_title", "Job title", "text"),
    (re.compile(r"salary|compensation|pay[\s_]*expect|expected[\s_]*pay", re.I),
     "salary_expectation", "Salary expectation", "text"),
    (re.compile(r"cover[\s_]*letter|why.*(you|apply|interest)|message|about[\s_]*you|"
                r"introduce|motivation", re.I),
     "cover_letter", "Cover letter", "text"),
    (re.compile(r"notice[\s_]*period|availab|start[\s_]*date", re.I),
     "availability", "Availability", "text"),
    (re.compile(r"years?[\s_]*(of[\s_]*)?experience|experience", re.I),
     "experience", "Years of experience", "text"),
]


def _clean_field_text(raw: str) -> str:
    """A raw field name/id → a lowercase, separator-normalised string the rules
    match against. Splits camelCase and framework separators ($ [ ] . _ -) into
    spaces, drops the known noise tokens, collapses whitespace."""
    text = (raw or "").strip()
    # camelCase / PascalCase → spaced: "txtFirstName" → "txt First Name"
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    # framework separators → space
    text = re.sub(r"[$\[\]()._\-]+", " ", text)
    text = _FIELD_NOISE_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _slug_key(cleaned: str, raw: str) -> str:
    """Fallback key for an unrecognised field: a sanitised slug of its own
    cleaned name (or a hash-free stable stem of the raw name when cleaning left
    nothing). Never empty — normalize_key guarantees a usable token."""
    stem = re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_")
    if not stem:
        stem = re.sub(r"[^a-z0-9]+", "_", (raw or "").lower()).strip("_")
    stem = stem[:48] or "field"
    return normalize_key(stem)


def derive_field_identity(
    raw_field_name: str, value: str = ""
) -> tuple[str, str, str]:
    """Map a raw form field name to a (key, label, kind) for the autofill profile.

    The intelligence behind "ask once, remember forever": a mangled field name
    like "ctl00$ContentPlaceHolder1$txtEmail" or "applicant[first_name]" becomes
    ("email", "Email", "text") / ("first_name", "First name", "text"). An
    unrecognised field falls back to a readable slug of its own name so it is
    still saved (never dropped). `value` only influences KIND — a URL-shaped
    answer to an unmatched field is stored as a link. Deterministic; never
    raises."""
    try:
        cleaned = _clean_field_text(raw_field_name)
        for pattern, key, label, kind in _FIELD_RULES:
            if pattern.search(cleaned):
                # A matched text field whose ANSWER is clearly a URL is a link.
                if kind == "text" and _URLISH_RE.search((value or "").strip()):
                    kind = "link"
                return key, label, kind
        # Unrecognised: keep it, keyed + labelled from its own cleaned name.
        key = _slug_key(cleaned, raw_field_name)
        label = (cleaned or key.replace("_", " ")).strip().title() or "Form field"
        kind = "link" if _URLISH_RE.search((value or "").strip()) else "text"
        return key, label[:120], kind
    except Exception:  # pragma: no cover - defensive; a bad name must never crash
        return normalize_key(raw_field_name) or "field", "Form field", "text"


# Answers that mean "I've handled it myself / move on" — NOT a value to store.
# Kept exact-match (not a fuzzy affirmative) so a real value that merely starts
# with "yes" (e.g. an email "yes.man@x.com") is never mistaken for a skip.
_SKIP_ANSWERS = frozenset(
    {"continue", "skip", "next", "done", "go", "proceed", "ok", "okay", ""}
)


def answer_is_skip(answer: str) -> bool:
    """True when a fill answer is a bare 'continue'/'skip' (the user added the
    value in Settings, or is moving on) rather than a value to learn and save."""
    return (answer or "").strip().lower() in _SKIP_ANSWERS


# ------------------------------------------------------------------ snapshot
@dataclass
class FillProfile:
    """A DB-free snapshot of the autofill profile the browse loop is handed.

    Secrets live in `_secrets` (real values, code-only) and appear NOWHERE the
    LLM can see them — `entries`/`prompt_block` carry the non-secret fields
    only; a secret is referenced by its placeholder and resolved at fill time
    (resolve_secret_ref). This is what keeps a sensitive value out of every LLM
    prompt and out of chat history."""

    entries: list[dict] = field(default_factory=list)  # non-secret {key,label,value,kind}
    secret_keys: list[str] = field(default_factory=list)
    _secrets: dict[str, str] = field(default_factory=dict)
    documents: dict[str, str] = field(default_factory=dict)  # key -> path (kind==document)

    @property
    def is_empty(self) -> bool:
        return not self.entries and not self.secret_keys

    def grounding_values(self) -> list[str]:
        """The trusted values a form fill may be grounded in — every non-secret
        field value plus each document's basename (so an upload_path drawn from a
        profile document still grounds). Secret VALUES are deliberately excluded:
        a secret is filled by code substitution, never by the grounding test."""
        vals = [e["value"] for e in self.entries if str(e.get("value") or "").strip()]
        for path in self.documents.values():
            base = _basename(path)
            if base:
                vals.append(base)
        return vals

    def secret_value(self, key: str) -> Optional[str]:
        return self._secrets.get(normalize_key(key))

    def resolve_secret_ref(self, text: str) -> Optional[str]:
        """If `text` is a "{{secret:<key>}}" reference to a known secret, the real
        value to fill; else None. The ONLY place a secret value is produced, and
        it is produced in code — never seen by the model that emitted the
        reference."""
        match = _SECRET_REF_RE.search(text or "")
        if not match:
            return None
        return self._secrets.get(normalize_key(match.group(1)))

    def prompt_block(self) -> str:
        """The 'YOUR PROFILE' block for the decision prompt: non-secret fields
        with their exact values, plus secret KEYS the model may reference by
        placeholder (never their values). '' when the profile is empty."""
        if self.is_empty:
            return ""
        lines = ["YOUR PROFILE (the user's own data — use these exact values to fill matching form fields):"]
        for e in self.entries:
            tag = " (a file to upload)" if e.get("kind") == "document" else ""
            lines.append(f"- {e['label']}{tag}: {e['value']}")
        if self.secret_keys:
            lines.append(
                "Sensitive values are hidden from you — to fill one, type its "
                "placeholder EXACTLY and the real value is substituted in code:"
            )
            for key in self.secret_keys:
                lines.append(f"- {key}: type {secret_ref(key)}")
        return "\n".join(lines)


def _to_entry(row: AutofillField) -> dict:
    return {"key": row.key, "label": row.label, "value": row.value, "kind": row.kind}


def to_snapshot(rows: list[AutofillField]) -> FillProfile:
    profile = FillProfile()
    for row in rows:
        if row.kind == "secret":
            profile.secret_keys.append(row.key)
            profile._secrets[row.key] = row.value
            continue
        profile.entries.append(_to_entry(row))
        if row.kind == "document":
            profile.documents[row.key] = row.value
    return profile


# -------------------------------------------------------------- accessors
async def list_fields(db: AsyncSession) -> list[AutofillField]:
    result = await db.execute(select(AutofillField).order_by(AutofillField.label))
    return list(result.scalars().all())


async def get_field(db: AsyncSession, key: str) -> Optional[AutofillField]:
    key = normalize_key(key)
    result = await db.execute(select(AutofillField).where(AutofillField.key == key))
    return result.scalar_one_or_none()


def _validate(label: str, value: str, kind: str) -> None:
    """Raise ValueError with a user-facing reason on a bad field. A DOCUMENT
    value is a file path and reuses the file-tools path safety (the upload
    grounding's own upload_path_unsafe — no system/protected/nonexistent file),
    so a document can never point the uploader at an arbitrary path."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if not (label or "").strip():
        raise ValueError("label is required")
    if not (value or "").strip():
        raise ValueError("value is required")
    if kind == "document":
        from app.agents.browser_grounding import upload_path_unsafe

        reason = upload_path_unsafe(value)
        if reason:
            raise ValueError(reason)


async def upsert_field(
    db: AsyncSession, key: str, label: str, value: str, kind: str = "text"
) -> AutofillField:
    """Create or replace a profile field by key. UPSERT on the unique key (an
    edit replaces the value, never a duplicate). Validates the kind and, for a
    document, the path safety — raising ValueError (the API turns it into a
    400)."""
    key = normalize_key(key)
    if not key:
        raise ValueError("key is required")
    kind = (kind or "text").strip().lower()
    _validate(label, value, kind)

    row = await get_field(db, key)
    if row is None:
        row = AutofillField(key=key, label=label.strip(), value=value.strip(), kind=kind)
        db.add(row)
    else:
        row.label = label.strip()
        row.value = value.strip()
        row.kind = kind
    await db.commit()
    await db.refresh(row)
    return row


async def delete_field(db: AsyncSession, key: str) -> bool:
    row = await get_field(db, key)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True


async def load_profile(db: AsyncSession) -> FillProfile:
    """The DB-free snapshot the planner/loop use for form-fill grounding."""
    return to_snapshot(await list_fields(db))


async def default_profile() -> FillProfile:
    """load_profile on a fresh session — for callers not already holding one
    (the browse_commit tool, which runs off the request's db). Best-effort: any
    failure yields an empty profile, so form-filling degrades to 'ask the user',
    never a crash."""
    try:
        from app.db.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            return await load_profile(db)
    except Exception:  # pragma: no cover - defensive
        from loguru import logger

        logger.warning("autofill: could not load the profile — using an empty one")
        return FillProfile()
