"""
Jarvis OS — Memory Engine
Central class for all memory operations: store, recall, and context assembly.
Every chat message flows through build_context() before hitting the LLM.
"""
import json
import re
import uuid
import difflib
from datetime import date, datetime
from typing import Optional
from enum import Enum

from loguru import logger
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qdrant_models
from app.providers.base import LLMProvider

from app.core.config import settings
from app.db.models import (
    SemanticMemory,
    Contact,
    ContactInteraction,
    Episode,
    Preference,
    UserProfile,
    EntityEdge,
    utc_now,
)
from app.memory.embedder import embed_text

MIN_SCORE = settings.IDENTITY_MIN_SCORE
MIN_GAP = settings.IDENTITY_MIN_GAP

# Placeholders emitted by the extractor LLM inside dual-perspective facts.
# Python substitutes them AFTER identity resolution so free text never needs
# post-hoc name surgery.
CONTACT_PLACEHOLDER_RE = re.compile(r"\{CONTACT:([^}]*)\}")
USER_PLACEHOLDER = "{USER}"


def substitute_placeholders(
    text: str,
    user_name: Optional[str] = None,
    contact_mapping: Optional[dict] = None,
    default_contact_name: Optional[str] = None,
) -> str:
    """
    Replace {USER} and {CONTACT:<name-as-said>} placeholders in a fact string.

    contact_mapping maps lowercase name-as-said → resolved full name.
    default_contact_name replaces any {CONTACT:...} not found in the mapping
    (used after a parked fact's single unresolved name gets confirmed).
    Unmatched placeholders fall back to the name as the user said it.
    """
    if not text:
        return text
    text = text.replace(USER_PLACEHOLDER, user_name or "the user")
    mapping = {k.lower(): v for k, v in (contact_mapping or {}).items()}

    def _sub(match: re.Match) -> str:
        as_said = match.group(1).strip()
        return mapping.get(as_said.lower()) or default_contact_name or as_said

    return CONTACT_PLACEHOLDER_RE.sub(_sub, text)


def parse_event_date(value) -> Optional[date]:
    """Parse the extractor's YYYY-MM-DD event_date string; None if invalid."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


# Leading past-tense verbs → base form, for rewriting facts about FUTURE events
# as plans ("Went to fishing ... on 2026-08-01" → "Planning to go to fishing ...").
# The extractor prompt asks for plan phrasing directly; this is the deterministic
# safety net for when the LLM phrases a future event in past tense anyway.
_PAST_TO_PLAN = {
    "went": "go", "had": "have", "played": "play", "met": "meet",
    "visited": "visit", "attended": "attend", "saw": "see", "ate": "eat",
    "drank": "drink", "watched": "watch", "took": "take", "did": "do",
    "bought": "buy", "celebrated": "celebrate", "traveled": "travel",
    "travelled": "travel", "got": "get", "made": "make", "gave": "give",
    "joined": "join", "hosted": "host", "threw": "throw",
}


def normalize_future_phrasing(text: str, event_date: Optional[date], today: Optional[date] = None) -> str:
    """If the event is in the future but the text opens with a past-tense verb,
    rewrite the opening as a plan. Past/undated facts are returned unchanged."""
    if not text or not event_date:
        return text
    if event_date <= (today or datetime.now().date()):
        return text
    first, _, rest = text.strip().partition(" ")
    base = _PAST_TO_PLAN.get(first.lower())
    if not base:
        return text
    return f"Planning to {base} {rest}".strip()


def supersede_is_covered(old_text: str, new_text: str) -> bool:
    """
    An old fact may only be superseded (soft-deleted) when the replacement
    preserves ALL of its information: every word of the old text must appear
    in the new one. Blocks the two observed data-loss shapes: a "merged"
    replacement that silently dropped participants, and supersede requests
    whose replacement never got written at all.
    """
    old_tokens = set(re.findall(r"\w+", (old_text or "").lower()))
    new_tokens = set(re.findall(r"\w+", (new_text or "").lower()))
    return bool(old_tokens) and old_tokens <= new_tokens
from dataclasses import dataclass, field

class ResolutionStatus(Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"

@dataclass
class IdentityResult:
    status: ResolutionStatus
    contact: Optional[Contact] = None
    candidates: list[dict] = field(default_factory=list)
    best_score: float = 0.0
    second_score: float = 0.0
    gap: float = 0.0
    reason: str = ""

from typing import Literal
from app.memory.conversation_state import (
    CONVERSATION_SESSIONS, ActiveEntity, PendingResolution, PendingCreation,
    get_session, touch_session
)

@dataclass
class ResolvedEntity:
    id: str
    type: Literal["contact", "project"]
    name: str
    confidence: float

@dataclass
class RetrievedContext:
    resolved_entities: list[ResolvedEntity]
    pending_resolution: dict | None
    user_profile: dict
    semantic_memories: list
    preferences: list
    episodes: list
    contacts: list[Contact]  # Store raw contacts for format_context
    pending_creation: dict | None = None  # unknown person awaiting create-confirmation
    ambiguous_mentions: list = field(default_factory=list)  # names in the current message needing a "which one?"


import time
from rapidfuzz import fuzz, distance

def normalize_name(name: str) -> str:
    import re
    if not name:
        return ""
    name = name.lower()
    name = re.sub(r"'s\b", "", name)
    name = re.sub(r"[^\w\s]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name

def resolve_identity(extracted_name: str, contacts: list) -> list:
    normalized_extracted = normalize_name(extracted_name)
    if not normalized_extracted:
        return []
        
    extracted_tokens = normalized_extracted.split()
    results = []
    
    for c in contacts:
        norm_c = normalize_name(c.name)
        if not norm_c:
            continue
            
        if normalized_extracted == norm_c:
            results.append((c, 100.0))
            continue
            
        def combined_sim(a, b):
            lev = fuzz.ratio(a, b)
            jw = distance.JaroWinkler.normalized_similarity(a, b) * 100
            return (lev + jw) / 2.0
            
        full_score = combined_sim(normalized_extracted, norm_c)
        
        # 1. Forward Token Score (Query -> Contact)
        c_tokens = norm_c.split()
        fwd_scores = []
        for e_tok in extracted_tokens:
            best_tok_score = max([combined_sim(e_tok, c_tok) for c_tok in c_tokens]) if c_tokens else 0
            if best_tok_score < 60: best_tok_score = 0
            fwd_scores.append(best_tok_score)
        fwd_avg = sum(fwd_scores) / len(fwd_scores) if fwd_scores else 0
        
        # 2. Reverse Token Score (Contact -> Query)
        rev_scores = []
        for c_tok in c_tokens:
            best_tok_score = max([combined_sim(c_tok, e_tok) for e_tok in extracted_tokens]) if extracted_tokens else 0
            if best_tok_score < 60: best_tok_score = 0
            rev_scores.append(best_tok_score)
        rev_avg = sum(rev_scores) / len(rev_scores) if rev_scores else 0
        
        bidirectional_token_score = (fwd_avg + rev_avg) / 2.0
        
        # 3. Weighted Final Score
        # If the user typed a strict subset of the contact's name (e.g. "jamil" matching "jamil ali"),
        # fwd_avg will be ~100. We boost this to 95 so it falls within the MIN_GAP (8) of an exact match (100).
        # This forces the system to ask for clarification instead of silently picking the exact match.
        if fwd_avg > 95:
            final_score = max(95.0, (0.50 * bidirectional_token_score) + (0.50 * full_score))
        else:
            final_score = (0.50 * bidirectional_token_score) + (0.50 * full_score)
        
        results.append((c, final_score))
        
    results.sort(key=lambda x: x[1], reverse=True)
    return results

def identify_contact(extracted_name: str, contacts: list) -> IdentityResult:
    results = resolve_identity(extracted_name, contacts)
    
    # Filter candidates that pass the threshold
    passing_candidates = [(c, s) for c, s in results if s >= MIN_SCORE]
    
    if len(passing_candidates) == 0:
        return IdentityResult(
            status=ResolutionStatus.NOT_FOUND,
            reason=f"Top score {results[0][1] if results else 0} < MIN_SCORE ({MIN_SCORE})"
        )
        
    best_candidate, best_score = passing_candidates[0]

    # EXACT MATCH RULE: a perfect score (100.0) means the user typed a contact's
    # exact name — but that is only unambiguous when NO other contact's name
    # contains all of its words plus more. "jamil" is an exact match for the
    # contact 'jamil', yet with 'Jamil Ali' and 'jamil ali khan' saved the user
    # may mean any of them — never guess, ask.
    if best_score == 100.0:
        exact_tokens = set(normalize_name(extracted_name).split())
        supersets = [
            c for c, s in results
            if c.id != best_candidate.id
            and exact_tokens < set(normalize_name(c.name).split())
        ]
        if not supersets:
            return IdentityResult(
                status=ResolutionStatus.RESOLVED,
                contact=best_candidate,
                best_score=best_score,
                reason="Exact name match with no longer variants — resolved unambiguously"
            )
        candidate_list = [best_candidate] + supersets
        return IdentityResult(
            status=ResolutionStatus.AMBIGUOUS,
            candidates=[{"id": c.id, "name": c.name} for c in candidate_list],
            best_score=best_score,
            reason=(
                f"Exact match '{best_candidate.name}' is a subset of "
                f"{len(supersets)} longer contact name(s) — confirmation required"
            ),
        )
    
    if len(passing_candidates) > 1:
        return IdentityResult(
            status=ResolutionStatus.AMBIGUOUS,
            candidates=[{"id": c.id, "name": c.name} for c, s in passing_candidates],
            best_score=best_score,
            second_score=passing_candidates[1][1],
            gap=best_score - passing_candidates[1][1],
            reason=f"Multiple contacts ({len(passing_candidates)}) passed MIN_SCORE threshold of {MIN_SCORE}"
        )
            
    return IdentityResult(
        status=ResolutionStatus.RESOLVED,
        contact=best_candidate,
        best_score=best_score,
        reason="Only 1 contact passed MIN_SCORE threshold — resolved unambiguously"
    )

def _find_embedded_name(reply: str, named_items: list) -> Optional[dict]:
    """
    Find a contact whose full name appears word-bounded inside the reply
    ("I meant jamil ali" → Jamil Ali). Returns the single longest match,
    or None if there is no match or the longest match is tied (ambiguous).
    """
    padded_reply = f" {normalize_name(reply)} "
    contained = [
        item for item in named_items
        if normalize_name(item["name"]) and f" {normalize_name(item['name'])} " in padded_reply
    ]
    if not contained:
        return None
    contained.sort(key=lambda item: len(normalize_name(item["name"])), reverse=True)
    longest = len(normalize_name(contained[0]["name"]))
    ties = [i for i in contained if len(normalize_name(i["name"])) == longest]
    # Entries that are the same name (e.g. a candidate dict plus its roster
    # entry) are not a real tie — earlier entries (candidates) win. Two
    # DIFFERENT names of equal length are genuinely ambiguous.
    tied_names = {normalize_name(i["name"]) for i in ties}
    return ties[0] if len(tied_names) == 1 else None


def resolve_confirmation(clarified_name: str, candidate_dicts: list, all_contacts: list) -> str | int | None:
    clarified_lower = clarified_name.strip().lower()

    # Check A: Positional
    if clarified_lower in ["the first one", "first one", "first", "1", "1st"]:
        if len(candidate_dicts) >= 1:
            return candidate_dicts[0]["id"]
    if clarified_lower in ["the second one", "second one", "second", "2", "2nd"]:
        if len(candidate_dicts) >= 2:
            return candidate_dicts[1]["id"]

    # Check A2: Contact name embedded in a sentence ("I meant jamil ali khan").
    # Fuzzy scoring punishes the extra words, so check containment first.
    # Search candidates AND the whole contact list together: the user may name
    # a contact that was not among the offered candidates, and a longer global
    # name ("jamil ali khan") must beat a shorter candidate ("jamil ali")
    # embedded inside it.
    seen_ids = {c["id"] for c in candidate_dicts}
    searchable = list(candidate_dicts) + [
        {"id": c.id, "name": c.name} for c in all_contacts if c.id not in seen_ids
    ]
    embedded = _find_embedded_name(clarified_name, searchable)
    if embedded:
        return embedded["id"]

    # Check B: Candidate Exact/Fuzzy Match
    class MockContact:
        def __init__(self, id, name):
            self.id = id
            self.name = name

    mock_candidates = [MockContact(c["id"], c["name"]) for c in candidate_dicts]
    candidate_result = identify_contact(clarified_name, mock_candidates)
    if candidate_result.status == ResolutionStatus.RESOLVED:
        return candidate_result.contact.id

    # Check C: Global Database Match
    # Only if the user typed something completely different (e.g. they corrected the name)
    global_result = identify_contact(clarified_name, all_contacts)
    if global_result.status == ResolutionStatus.RESOLVED:
        return global_result.contact.id

    # Check D: Not Found anywhere
    return "NOT_FOUND_GLOBAL"


def _scan_reply_contacts(reply: str, all_contacts: list) -> list:
    """
    Every contact whose full name appears word-bounded in the reply, longest
    names first, each text span consumed once ("ali raza" swallows "ali").
    """
    padded = f" {normalize_name(reply)} "
    found = []
    for contact in sorted(all_contacts, key=lambda c: len(normalize_name(c.name)), reverse=True):
        norm = normalize_name(contact.name)
        if norm and f" {norm} " in padded:
            found.append(contact)
            padded = padded.replace(f" {norm} ", "  ", 1)
    return found


def resolve_confirmation_multi(reply: str, mentions: list, all_contacts: list) -> dict:
    """
    Resolve a confirmation reply that may answer SEVERAL pending ambiguous
    names at once ("i meant hamil and ali raza" answering both "ali" and
    "jamil"). Returns {as-said mention name → contact id} for every mention
    the reply settles; mentions it doesn't settle are simply absent.

    Assignment order:
      1. single pending mention → the full single-name resolver (positional
         answers, embedded names, fuzzy, global) — unchanged behavior.
      2. candidate membership — a contact named in the reply that is among a
         mention's offered candidates answers THAT mention ("ali raza" → "ali").
      3. fuzzy leftovers — remaining reply names pair with remaining mentions
         by name similarity ("hamil" → the mention "jamil"): the user corrected
         the name rather than picking a candidate.
    """
    if len(mentions) == 1:
        resolved = resolve_confirmation(reply, mentions[0].get("candidates", []), all_contacts)
        if resolved and resolved != "NOT_FOUND_GLOBAL":
            return {mentions[0]["name"]: resolved}
        return {}

    assignment: dict = {}
    pool = _scan_reply_contacts(reply, all_contacts)
    remaining = list(mentions)

    # Pass 1: candidate membership
    for mention in list(remaining):
        candidate_ids = {c["id"] for c in mention.get("candidates", [])}
        members = [c for c in pool if c.id in candidate_ids]
        if len(members) == 1:
            assignment[mention["name"]] = members[0].id
            pool.remove(members[0])
            remaining.remove(mention)

    # Pass 2: pair leftover reply names with leftover mentions by similarity
    while remaining and pool:
        best_score, best_pair = 0.0, None
        for mention in remaining:
            for contact in pool:
                score = fuzz.WRatio(normalize_name(mention["name"]), normalize_name(contact.name))
                if score > best_score:
                    best_score, best_pair = score, (mention, contact)
        if best_pair is None or best_score < 60:
            break
        mention, contact = best_pair
        assignment[mention["name"]] = contact.id
        pool.remove(contact)
        remaining.remove(mention)

    return assignment


class MemoryEngine:
    """
    The brain of Jarvis OS.
    Stores and retrieves all 5 memory types, then assembles them into a
    context block that gets injected into every system prompt.
    """

    def __init__(self, db: AsyncSession, qdrant: Optional[AsyncQdrantClient], provider=None):
        self.db = db
        self.qdrant = qdrant
        self.provider = provider  # LLMProvider — used for intelligent note merging

    # ==============================================================
    # STORE
    # ==============================================================

    async def store_semantic_memory(
        self,
        content: str,
        category: str = "fact",
        source: str = "extracted",
        confidence: float = 0.9,
        subject: str = "user",
        contact_id: Optional[str] = None,
        event_date: Optional[date] = None,
    ) -> SemanticMemory:
        """Store a factual memory about the user in SQLite + Qdrant.
        
        Before inserting, checks for semantic duplicates. If an existing active
        fact has cosine similarity > 0.92 (same meaning, different wording),
        the old fact is soft-deleted and replaced by the new one.
        """
        content = normalize_future_phrasing(content, event_date)
        # --- Exact-text dedup ---
        existing_result = await self.db.execute(
            select(SemanticMemory).where(
                SemanticMemory.content == content,
                SemanticMemory.is_active == True,
            )
        )
        existing = existing_result.scalars().first()
        if existing:
            logger.debug(f"Dedup (exact): memory already exists: {content[:60]}")
            return existing

        # --- Semantic similarity dedup via Qdrant ---
        if self.qdrant:
            try:
                vector = await embed_text(content)
                similar = await self.qdrant.search(
                    collection_name="semantic_memory",
                    query_vector=vector,
                    limit=3,
                    score_threshold=settings.SEMANTIC_DEDUP_THRESHOLD,  # Very high — only near-identical facts
                )
                if similar:
                    # Soft-delete all near-duplicate existing facts
                    dup_ids = [hit.id for hit in similar]
                    dup_result = await self.db.execute(
                        select(SemanticMemory).where(
                            SemanticMemory.id.in_(dup_ids),
                            SemanticMemory.is_active == True,
                        )
                    )
                    for dup in dup_result.scalars().all():
                        logger.info(f"Dedup (semantic): replacing '{dup.content[:60]}' with newer fact")
                        dup.is_active = False
                    await self.db.flush()
            except Exception as e:
                logger.warning(f"Semantic dedup check failed (non-critical): {e}")
                vector = None
        else:
            vector = None

        memory = SemanticMemory(
            content=content,
            category=category,
            source=source,
            confidence=confidence,
            subject=subject,
            contact_id=contact_id,
            event_date=event_date,
        )
        self.db.add(memory)
        await self.db.flush()  # Get ID before Qdrant upsert

        # Store vector embedding (reuse vector if already computed above)
        if self.qdrant:
            try:
                if vector is None:
                    vector = await embed_text(content)
                await self.qdrant.upsert(
                    collection_name="semantic_memory",
                    points=[
                        qdrant_models.PointStruct(
                            id=memory.id,
                            vector=vector,
                            payload={
                                "content": content,
                                "category": category,
                                "source": source,
                            },
                        )
                    ],
                )
                memory.qdrant_id = memory.id
            except Exception as e:
                logger.warning(f"Qdrant upsert failed for semantic memory: {e}")

        await self.db.commit()
        await self.db.refresh(memory)
        logger.info(f"Stored semantic memory [{category}]: {content[:80]}")
        return memory

    async def _resolve_collaborator_ids(self, collabs: list[str]) -> list[str]:
        """Convert a list of names or IDs into a pure list of Contact IDs, creating contacts if needed."""
        resolved_ids = []
        for c in collabs:
            c_str = str(c).strip()
            if not c_str: continue
            if len(c_str) == 36 and "-" in c_str: # looks like UUID
                resolved_ids.append(c_str)
            else:
                contact = await self.store_contact(c_str)
                resolved_ids.append(contact.id)
        # Deduplicate while preserving order
        seen = set()
        unique_ids = []
        for cid in resolved_ids:
            if cid not in seen:
                unique_ids.append(cid)
                seen.add(cid)
        return unique_ids


    async def create_contact_manual(
        self, name: str, details: Optional[dict] = None
    ) -> Contact:
        """Manually create a contact with duplicate prevention."""
        details = details or {}
        
        # Check for existing duplicate (fuzzy match)
        result = await self.db.execute(
            select(Contact).where(Contact.is_active == True)
        )
        existing_contacts = result.scalars().all()
        
        norm_name = normalize_name(name)
        for c in existing_contacts:
            norm_c = normalize_name(c.name)
            if not norm_c: continue
            
            score = fuzz.ratio(norm_name, norm_c)
            if score == 100:  # Require exact match (case-insensitive) for manual duplicates
                raise ValueError(f"A similar contact already exists: {c.name}")
        
        contact = Contact(
            name=name,
            email=details.get("email"),
            phone=details.get("phone"),
            organization=details.get("organization"),
            relationship_type=details.get("relationship_type", "other"),
            notes=details.get("notes"),
        )
        if "skills" in details and isinstance(details["skills"], list):
            contact.skills = json.dumps(details["skills"])
        if "important_dates" in details and isinstance(details["important_dates"], dict):
            contact.important_dates = json.dumps(details["important_dates"])
            
        self.db.add(contact)
        await self.db.flush()
        
        if self.qdrant:
            try:
                vector = await embed_text(name)
                await self.qdrant.upsert(
                    collection_name="contacts",
                    points=[
                        qdrant_models.PointStruct(
                            id=contact.id,
                            vector=vector,
                            payload={"name": name, **details},
                        )
                    ],
                )
                contact.qdrant_id = contact.id
            except Exception as e:
                logger.warning(f"Qdrant upsert failed for manually created contact: {e}")
                
        await self.db.commit()
        await self.db.refresh(contact)
        logger.info(f"Manually created contact: '{name}'")
        return contact

    async def store_contact(
        self, name: str, details: Optional[dict] = None, session_id: Optional[str] = None
    ) -> Optional[Contact]:
        """Create or update a contact. Returns the contact record."""
        details = details or {}

        # Check for existing contact by name (exact or fuzzy match)
        result = await self.db.execute(
            select(Contact).where(Contact.is_active == True)
        )
        existing_contacts = result.scalars().all()
        
        # 1. Check Pending Cache
        pending = None
        if session_id:
            sess = get_session(session_id)
            pending_res = sess.pending_resolution
            if pending_res is not None:
                # Expire old entries
                if time.time() > pending_res.expires:
                    sess.pending_resolution = None
                    pending_res = None
                else:
                    pending = pending_res
                    confirmed_id = resolve_confirmation(name, pending.candidates, existing_contacts)
                    if confirmed_id and confirmed_id != "NOT_FOUND_GLOBAL":
                        # Found in pending! Merge facts and update
                        logger.info(f"store_contact: Resolved pending confirmation to ID {confirmed_id}")
                        # Merge new details into pending update
                        merged_fact = pending.pending_update.copy()
                        merged_fact.update(details)
                        # Update contact and clear pending
                        sess.pending_resolution = None
                        touch_session(session_id)
                        return await self.update_contact(confirmed_id, merged_fact)
                    else:
                        logger.info(f"store_contact: Confirmation '{name}' failed against pending candidates. Falling back to full search.")
                        # We intentionally do NOT merge pending.pending_update into details here!
                        # The backend cannot semantically distinguish between a name correction ("I meant Hamil") 
                        # and a completely new topic ("Mary's email is...").
                        # If this is a correction, the LLM Extractor will have re-extracted the facts into `details`.
                        
                        # We intentionally do NOT delete the pending cache here.
                        # It should stay alive in case this was just a new topic,
                        # so the pending facts aren't lost if the user returns to it.

        # 2. Resolve Identity
        result = identify_contact(name, existing_contacts)

        if result.status == ResolutionStatus.NOT_FOUND:
            # Unknown person — never create silently. Park the details so the
            # next turn can ask "X isn't in your contacts — want me to add them?"
            # Only park when there is actual information to save; an empty
            # mention isn't worth interrupting the user for.
            has_signal = any(v for v in details.values())
            if session_id and has_signal:
                self._park_pending_creation(session_id, name, pending_update=details)
                logger.info(f"store_contact: Contact '{name}' not found. Parked for creation confirmation.")
            else:
                logger.info(f"store_contact: Contact '{name}' not found and nothing to save. {result.reason}")
            return None
            
        if result.status == ResolutionStatus.AMBIGUOUS:
            if session_id:
                logger.info(f"store_contact: Ambiguous resolution for '{name}'. {result.reason}. Storing in pending cache.")
                sess = get_session(session_id)
                sess.pending_resolution = PendingResolution(
                    original_name=name,
                    pending_update=details,
                    candidates=result.candidates
                )
                touch_session(session_id)
            else:
                logger.warning(f"store_contact: Ambiguous resolution for '{name}' but no session_id provided. Dropping fact.")
            return None
                
        # 3. Update existing
        best_candidate = result.contact
        logger.info(f"store_contact: Merging '{name}' into '{best_candidate.name}' (Score: {result.best_score})")
        
        # If we successfully resolved the contact after a fallback from a pending resolution, clear the cache.
        if pending and session_id:
            get_session(session_id).pending_resolution = None
            touch_session(session_id)
            
        return await self.update_contact(best_candidate.id, details)

    async def update_contact(self, contact_id: str, updates: dict) -> Contact:
        """Update fields on an existing contact."""
        result = await self.db.execute(
            select(Contact).where(Contact.id == contact_id)
        )
        contact = result.scalar_one_or_none()
        if not contact:
            raise ValueError(f"Contact {contact_id} not found")

        # Scalar fields — only overwrite if new value is provided and different
        for key, field in {
            "email": "email",
            "phone": "phone",
            "organization": "organization",
            "relationship_type": "relationship_type",
            "relationship": "relationship_type",
            "birthday": "birthday",
        }.items():
            if key in updates and updates[key]:
                setattr(contact, field, updates[key])

        # Skills — merge (case-insensitive dedup)
        new_skills = updates.get("skills")
        if new_skills and isinstance(new_skills, list):
            existing_skills = json.loads(contact.skills or "[]")
            seen = {s.lower() for s in existing_skills}
            for sk in new_skills:
                if sk.lower() not in seen:
                    existing_skills.append(sk)
                    seen.add(sk.lower())
            contact.skills = json.dumps(existing_skills)

        contact.interaction_count += 1
        contact.last_interaction = utc_now()
        contact.updated_at = utc_now()

        # Add new facts as interactions — dedup by description to prevent repeated extraction
        new_facts = updates.get("new_facts", [])
        user_name = await self.get_user_name() if new_facts else None
        for fact in new_facts:
            description = substitute_placeholders(fact.get("fact", ""), user_name=user_name)
            await self.add_contact_fact(
                contact,
                description,
                category=fact.get("category", "other"),
                event_date=parse_event_date(fact.get("event_date")),
            )

        await self.db.commit()
        await self.db.refresh(contact)
        return contact

    async def add_contact_fact(
        self,
        contact: Contact,
        description: str,
        category: str = "other",
        event_date: Optional[date] = None,
    ) -> bool:
        """
        Add one fact to a contact's interaction log, skipping exact duplicates
        (case-insensitive description match). Does NOT commit — callers own
        the transaction. Returns True if a row was added.
        """
        description = normalize_future_phrasing((description or "").strip(), event_date)
        if not description:
            return False
        existing_result = await self.db.execute(
            select(ContactInteraction.description).where(
                ContactInteraction.contact_id == contact.id
            )
        )
        existing_descriptions = {row[0].strip().lower() for row in existing_result.fetchall()}
        if description.lower() in existing_descriptions:
            logger.debug(f"add_contact_fact: Skipping duplicate fact: '{description[:60]}'")
            return False
        self.db.add(ContactInteraction(
            contact_id=contact.id,
            description=description,
            category=category,
            event_date=event_date,
        ))
        return True

    # ==============================================================
    # SHARED FACTS — dual-perspective writes (user's About Me + contact log)
    # ==============================================================

    async def get_user_name(self) -> Optional[str]:
        """The user's name from UserProfile, or None if not yet known."""
        profile = await self.get_user_profile()
        return profile.name if profile and profile.name else None

    @staticmethod
    def _merge_pending_update(existing: dict, new: dict) -> None:
        """Merge deferred contact updates in place; new_facts lists append."""
        for key, value in (new or {}).items():
            if not value:
                continue
            if key == "new_facts" and isinstance(existing.get(key), list):
                existing[key].extend(value)
            else:
                existing[key] = value

    def _park_pending_creation(
        self,
        session_id: str,
        name: str,
        pending_update: Optional[dict] = None,
        shared_fact: Optional[dict] = None,
        resolved_so_far: Optional[dict] = None,
    ) -> None:
        """
        Defer writes for an unknown person until the user confirms creation.
        Only one creation question is held at a time (one clarifying question
        per turn); a second unknown name is dropped with a log.
        """
        sess = get_session(session_id)
        pc = sess.pending_creation
        if pc is not None and time.time() > pc.expires:
            sess.pending_creation = None
            pc = None
        if pc is None:
            pc = PendingCreation(name=name)
            sess.pending_creation = pc
        elif normalize_name(pc.name) != normalize_name(name):
            logger.info(
                f"_park_pending_creation: '{pc.name}' already pending; dropping second unknown '{name}'"
            )
            return
        if pending_update:
            self._merge_pending_update(pc.pending_update, pending_update)
        if shared_fact:
            pc.pending_shared_facts.append(shared_fact)
        pc.resolved_so_far.update(resolved_so_far or {})
        touch_session(session_id)

    def _park_pending_shared_resolution(
        self,
        session_id: str,
        mentions: list,
        shared_fact: dict,
        resolved_so_far: Optional[dict] = None,
    ) -> None:
        """
        Defer a shared fact behind the session's disambiguation question.
        mentions holds EVERY ambiguous name in the fact ([{"name","candidates"}]);
        resolved_so_far carries the names that already resolved (as-said lower →
        contact id) so re-routing after confirmation never re-asks about them.
        """
        sess = get_session(session_id)
        pr = sess.pending_resolution
        if pr is not None and time.time() > pr.expires:
            sess.pending_resolution = None
            pr = None
        if pr is None:
            pr = PendingResolution(
                original_name=mentions[0]["name"],
                pending_update={},
                candidates=mentions[0]["candidates"],
                unresolved_mentions=list(mentions),
            )
            sess.pending_resolution = pr
        else:
            if not pr.unresolved_mentions:
                pr.unresolved_mentions = [
                    {"name": pr.original_name, "candidates": pr.candidates}
                ]
            known = {normalize_name(m["name"]) for m in pr.unresolved_mentions}
            for mention in mentions:
                if normalize_name(mention["name"]) not in known:
                    pr.unresolved_mentions.append(mention)
                    known.add(normalize_name(mention["name"]))
        pr.resolved_so_far.update(resolved_so_far or {})
        pr.pending_shared_facts.append(shared_fact)
        touch_session(session_id)

    @staticmethod
    def _substitute_fact(fact: dict, mapping: dict) -> dict:
        """Return a copy of a shared-fact dict with resolved contact names substituted."""
        out = dict(fact)
        for key in ("fact_user_perspective", "fact_contact_perspective"):
            text = out.get(key) or ""
            out[key] = CONTACT_PLACEHOLDER_RE.sub(
                lambda m: (
                    "{CONTACT:" + m.group(1) + "}"
                    if m.group(1).strip().lower() not in mapping
                    else "{CONTACT:" + mapping[m.group(1).strip().lower()] + "}"
                ),
                text,
            )
        return out

    async def _write_shared_fact(
        self,
        fact: dict,
        contacts: list[Contact],
        contact_mapping: Optional[dict] = None,
        default_contact_name: Optional[str] = None,
    ) -> str:
        """
        Write both perspectives of a fully-resolved shared fact:
        user perspective → SemanticMemory (About Me), contact perspective →
        each contact's interaction log.

        Returns the substituted user-perspective text so callers can tell the
        LLM exactly what was saved just now (it will already show up in the
        memory context of the same turn and must not read as an old memory).
        """
        user_name = await self.get_user_name()
        event_date = parse_event_date(fact.get("event_date"))
        category = fact.get("category") or "fact"

        user_side = substitute_placeholders(
            fact.get("fact_user_perspective", ""),
            user_name=user_name,
            contact_mapping=contact_mapping,
            default_contact_name=default_contact_name,
        )
        user_side = normalize_future_phrasing(user_side, event_date)
        if user_side:
            await self.store_semantic_memory(
                user_side,
                category=category,
                source="extracted",
                subject="shared" if contacts else fact.get("subject", "user"),
                contact_id=contacts[0].id if contacts else None,
                event_date=event_date,
            )
            await self.apply_supersede_candidates(fact, user_side)

        added = False
        for contact in contacts:
            contact_side = self._derive_contact_side(
                fact.get("fact_user_perspective", ""),
                contact,
                user_name=user_name,
                contact_mapping=contact_mapping,
                default_contact_name=default_contact_name,
            )
            if contact_side is None:
                # Derivation impossible — fall back to the LLM's own perspective
                # flip, with guards against the known misfill shapes.
                contact_side = substitute_placeholders(
                    fact.get("fact_contact_perspective", ""),
                    user_name=user_name,
                    contact_mapping=contact_mapping,
                    default_contact_name=default_contact_name,
                )
                if contact_side and user_side and contact_side.strip().lower() == user_side.strip().lower():
                    logger.debug("Skipping contact-side write: perspective not flipped by extractor")
                    contact_side = ""
                # A contact's own log should not name them (misfilled user
                # perspective), and the user's name must not appear more than
                # once ("Khawar went fishing with Khawar and Khawar").
                if contact_side and contact.name.lower() in contact_side.lower():
                    logger.debug(
                        f"Skipping contact-side write for '{contact.name}': text is self-referential"
                    )
                    contact_side = ""
                if contact_side and user_name and contact_side.lower().count(user_name.lower()) > 1:
                    logger.debug(
                        "Skipping contact-side write: extractor filled multiple people as the user"
                    )
                    contact_side = ""
            if not contact_side:
                continue
            if await self.add_contact_fact(
                contact, contact_side, category=category, event_date=event_date
            ):
                contact.interaction_count += 1
                contact.last_interaction = utc_now()
                added = True
                logger.info(
                    f"Shared fact written to '{contact.name}' log: '{contact_side[:60]}'"
                )
        if added:
            await self.db.commit()
        return user_side

    @staticmethod
    def _derive_contact_side(
        user_perspective: str,
        contact: Contact,
        user_name: Optional[str],
        contact_mapping: Optional[dict] = None,
        default_contact_name: Optional[str] = None,
    ) -> Optional[str]:
        """
        Deterministically flip the user-perspective template for one contact's
        log: that contact's {CONTACT:...} placeholder becomes the user's name,
        every other placeholder resolves normally. "Went fishing with
        {CONTACT:ali} and {CONTACT:jamil}" → on hamil's log: "Went fishing with
        Ali Raza and Khawar". This never trusts the LLM's perspective flip.

        Returns None when the flip can't be done safely: the template names the
        user explicitly via {USER} (the swap would double the user up), or no
        placeholder in it refers to this contact.
        """
        if not user_perspective or USER_PLACEHOLDER in user_perspective:
            return None
        mapping = {k.lower(): v for k, v in (contact_mapping or {}).items()}
        contact_norm = normalize_name(contact.name)
        swapped = False

        def _sub(match: re.Match) -> str:
            nonlocal swapped
            as_said = match.group(1).strip()
            resolved = mapping.get(as_said.lower()) or default_contact_name or as_said
            if normalize_name(resolved) == contact_norm or normalize_name(as_said) == contact_norm:
                swapped = True
                return user_name or "the user"
            return resolved

        flipped = CONTACT_PLACEHOLDER_RE.sub(_sub, user_perspective)
        return flipped if swapped else None

    async def store_shared_fact(
        self,
        fact: dict,
        session_id: Optional[str] = None,
        preresolved: Optional[dict] = None,
    ) -> Optional[str]:
        """
        Route one extracted fact-about-user through identity resolution.

        subject == "user" → single About-Me write.
        subject == "shared" → resolve every related contact; if all resolve,
        write both perspectives. ALL ambiguous names park together behind ONE
        disambiguation question (never one at a time); a NOT_FOUND name parks
        behind a create-contact question once no ambiguity remains.

        preresolved maps as-said names (lowercased) → Contact for names the
        user has already confirmed in this session — those are never
        re-resolved (re-resolving a confirmed "Jamil Ali" would flag it
        ambiguous against "jamil ali khan" all over again).

        Returns the saved user-perspective text, or None if the fact was
        parked (or empty).
        """
        user_text = (fact.get("fact_user_perspective") or "").strip()
        if not user_text:
            return None
        related = fact.get("related_contacts") or []

        if fact.get("subject") != "shared" or not related:
            user_name = await self.get_user_name()
            text = substitute_placeholders(user_text, user_name=user_name)
            memory = await self.store_semantic_memory(
                text,
                category=fact.get("category") or "fact",
                source="extracted",
                subject="user",
                event_date=parse_event_date(fact.get("event_date")),
            )
            saved_text = memory.content if memory else text
            await self.apply_supersede_candidates(fact, saved_text)
            return saved_text

        all_contacts = await self.get_all_contacts()
        preresolved = {k.lower(): v for k, v in (preresolved or {}).items()}
        mapping: dict = {}
        resolved: list[Contact] = []
        ambiguous_mentions: list[dict] = []
        unknown_names: list[str] = []
        resolved_ids: dict = {}  # as-said (lower) → contact id, for re-parking
        for name in related:
            confirmed = preresolved.get(name.lower())
            if confirmed is not None:
                mapping[name.lower()] = confirmed.name
                resolved.append(confirmed)
                resolved_ids[name.lower()] = confirmed.id
                continue
            result = identify_contact(name, all_contacts)
            if result.status == ResolutionStatus.RESOLVED:
                mapping[name.lower()] = result.contact.name
                resolved.append(result.contact)
                resolved_ids[name.lower()] = result.contact.id
            elif result.status == ResolutionStatus.AMBIGUOUS:
                ambiguous_mentions.append({"name": name, "candidates": result.candidates})
            else:
                unknown_names.append(name)
        if ambiguous_mentions:
            if session_id:
                parked = self._substitute_fact(fact, mapping)
                self._park_pending_shared_resolution(
                    session_id, ambiguous_mentions, parked, resolved_so_far=resolved_ids
                )
                names = ", ".join(m["name"] for m in ambiguous_mentions)
                logger.info(f"store_shared_fact: ambiguous name(s) [{names}] — fact parked for disambiguation")
            else:
                logger.warning("store_shared_fact: ambiguous name(s), no session — fact dropped")
            return None
        if unknown_names:
            if session_id:
                parked = self._substitute_fact(fact, mapping)
                self._park_pending_creation(
                    session_id, unknown_names[0], shared_fact=parked, resolved_so_far=resolved_ids
                )
                logger.info(
                    f"store_shared_fact: '{unknown_names[0]}' unknown — fact parked for creation confirmation"
                )
            else:
                logger.warning(f"store_shared_fact: '{unknown_names[0]}' unknown, no session — fact dropped")
            return None

        return await self._write_shared_fact(fact, resolved, contact_mapping=mapping)

    async def apply_shared_fact_to_contact(self, fact: dict, contact: Contact) -> str:
        """
        Apply a parked dual-perspective fact after its one unresolved name has
        been confirmed as `contact` (remaining placeholders all refer to them).
        Returns the saved user-perspective text.
        """
        return await self._write_shared_fact(fact, [contact], default_contact_name=contact.name)

    async def apply_shared_fact_user_only(self, fact: dict, as_said_name: Optional[str] = None) -> str:
        """
        User declined to create the contact: keep only the About-Me side.
        Every unmapped {CONTACT:...} placeholder falls back to the name exactly
        as it was said — never a single default slammed over all of them (a
        multi-person fact may mix the declined name with already-resolved ones).
        """
        user_name = await self.get_user_name()
        text = substitute_placeholders(
            fact.get("fact_user_perspective", ""),
            user_name=user_name,
        )
        text = normalize_future_phrasing(text, parse_event_date(fact.get("event_date")))
        if text:
            await self.store_semantic_memory(
                text,
                category=fact.get("category") or "fact",
                source="extracted",
                subject="user",
                event_date=parse_event_date(fact.get("event_date")),
            )
        return text

    async def get_all_contact_names(self) -> list[str]:
        """Return all active contact names for the extractor to use for disambiguation."""
        result = await self.db.execute(
            select(Contact.name).where(Contact.is_active == True)
        )
        return [row[0] for row in result.fetchall()]

    async def delete_contact_by_name(self, name: str) -> bool:
        """Hard-delete a contact by their exact name. Used to clean up mistakenly created contacts."""
        result = await self.db.execute(
            select(Contact).where(func.lower(Contact.name) == name.lower())
        )
        contacts = list(result.scalars().all())
        for contact in contacts:
            await self.db.delete(contact)
        if contacts:
            await self.db.commit()
            logger.info(f"Deleted mistakenly created contact: '{name}' ({len(contacts)} row(s))")
            return True
        return False

    async def apply_supersede_candidates(self, fact: dict, new_text: str) -> None:
        """
        Apply the extractor's supersede requests attached to a fact
        (fact["_supersede_candidates"]) AFTER its user-side text was actually
        written. Each old fact is deleted only if the new text covers it —
        see supersede_is_covered. Requests travel with parked facts, so a
        replacement deferred behind a "which X?" question supersedes the old
        fact only once it finally lands, never before.
        """
        if not new_text:
            return
        for old_text in fact.get("_supersede_candidates") or []:
            if old_text.strip().lower() == new_text.strip().lower():
                continue  # never delete the fact that was just (re)written
            if supersede_is_covered(old_text, new_text):
                await self.delete_semantic_memory_by_content(old_text)
            else:
                logger.info(
                    f"Supersede blocked (new fact does not cover it): '{old_text[:60]}'"
                )

    async def delete_semantic_memory(self, memory_id: str) -> bool:
        """
        Hard-delete a semantic memory by id (user-initiated, e.g. the trash
        button in About Me). Unlike the supersede path (which only soft-deletes
        so history is recoverable), a user deletion removes the SQLite row AND
        the Qdrant point — the fact must be gone from the database, not hidden.
        Deleting the row also clears the exact-text dedup, so the user can
        re-add the same fact later.
        """
        result = await self.db.execute(
            select(SemanticMemory).where(SemanticMemory.id == memory_id)
        )
        memory = result.scalar_one_or_none()
        if not memory:
            return False
        content_preview = memory.content[:60]
        await self.db.delete(memory)
        await self.db.commit()

        # Best-effort Qdrant cleanup AFTER the SQLite delete: a stale point is
        # harmless (search maps hits back through SQLite rows), a stale row is not.
        if self.qdrant:
            try:
                await self.qdrant.delete(
                    collection_name="semantic_memory",
                    points_selector=qdrant_models.PointIdsList(points=[memory_id]),
                )
            except Exception as e:
                logger.warning(f"Qdrant point delete failed for memory {memory_id} (non-critical): {e}")

        logger.info(f"Deleted semantic memory (user request): '{content_preview}'")
        return True

    async def delete_contact_fact(self, contact_id: str, interaction_id: str) -> bool:
        """
        Hard-delete one entry from a contact's fact log (user-initiated).
        The interaction must belong to the given contact. Decrements the
        contact's interaction_count. Contact facts have no Qdrant points,
        so the SQLite delete is the whole story. Commits.
        """
        result = await self.db.execute(
            select(ContactInteraction).where(
                ContactInteraction.id == interaction_id,
                ContactInteraction.contact_id == contact_id,
            )
        )
        interaction = result.scalar_one_or_none()
        if not interaction:
            return False
        description_preview = interaction.description[:60]
        await self.db.delete(interaction)

        contact_result = await self.db.execute(
            select(Contact).where(Contact.id == contact_id)
        )
        contact = contact_result.scalar_one_or_none()
        if contact and contact.interaction_count > 0:
            contact.interaction_count -= 1

        await self.db.commit()
        logger.info(f"Deleted contact fact (user request): '{description_preview}'")
        return True

    async def delete_semantic_memory_by_content(self, content: str) -> bool:
        """Soft-delete a semantic memory by its exact content. Used to supersede outdated facts."""
        result = await self.db.execute(
            select(SemanticMemory).where(
                SemanticMemory.content == content,
                SemanticMemory.is_active == True,
            )
        )
        memories = list(result.scalars().all())
        for memory in memories:
            memory.is_active = False
        if memories:
            await self.db.commit()
            logger.info(f"Superseded outdated fact: '{content[:60]}'")
            return True
        return False

    # ==============================================================
    # USER PROFILE (Phase 2.5 Lite)
    # ==============================================================

    async def store_user_profile(self, enrichments: dict) -> UserProfile:
        """
        Upsert the single-row UserProfile with new info.
        JSON array fields (skills, languages) are deduplicated and merged.
        All other fields are overwritten if the new value is non-null.
        """
        result = await self.db.execute(select(UserProfile))
        profile = result.scalars().first()

        if not profile:
            import uuid
            profile = UserProfile(id=str(uuid.uuid4()))
            self.db.add(profile)
            await self.db.flush()

        # Scalar fields — overwrite if provided
        for field in ["name", "profession", "location", "work_style", "background"]:
            val = enrichments.get(field)
            if val:
                setattr(profile, field, val)

        # JSON array fields — merge + dedup
        for field in ["skills", "languages"]:
            new_items = enrichments.get(field)
            if new_items and isinstance(new_items, list):
                existing = json.loads(getattr(profile, field) or "[]")
                seen = {x.lower() for x in existing}
                for item in new_items:
                    if item.lower() not in seen:
                        existing.append(item)
                        seen.add(item.lower())
                setattr(profile, field, json.dumps(existing))

        profile.updated_at = utc_now()
        await self.db.commit()
        await self.db.refresh(profile)
        logger.info(f"UserProfile updated: {list(enrichments.keys())}")
        return profile

    async def get_user_profile(self) -> Optional[UserProfile]:
        """Fetch the single UserProfile row (first row wins on legacy dupes)."""
        result = await self.db.execute(select(UserProfile))
        return result.scalars().first()

    # ==============================================================
    # ENTITY EDGES (Phase 2.5 Lite)
    # ==============================================================

    async def store_entity_edge(
        self,
        source_type: str,
        source_name: str,
        edge_type: str,
        target_type: str,
        target_name: str,
        source_id: Optional[str] = None,
        target_id: Optional[str] = None,
        confidence: float = 0.7,
        edge_label: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> EntityEdge:
        """
        Store a typed relationship edge, deduplicating by
        (source_name, edge_type, target_name). If the edge already exists,
        update its confidence to the max of old and new.
        """
        # Dedup check
        result = await self.db.execute(
            select(EntityEdge).where(
                func.lower(EntityEdge.source_name) == source_name.lower(),
                EntityEdge.edge_type == edge_type,
                func.lower(EntityEdge.target_name) == target_name.lower(),
                EntityEdge.is_active == True,
            )
        )
        existing = result.scalars().first()
        if existing:
            # Update confidence if new value is higher
            existing.confidence = max(existing.confidence, confidence)
            existing.updated_at = utc_now()
            if source_id and not existing.source_id:
                existing.source_id = source_id
            if target_id and not existing.target_id:
                existing.target_id = target_id
            await self.db.commit()
            return existing

        edge = EntityEdge(
            source_type=source_type,
            source_id=source_id,
            source_name=source_name,
            edge_type=edge_type,
            edge_label=edge_label,
            target_type=target_type,
            target_id=target_id,
            target_name=target_name,
            confidence=confidence,
            source_session=session_id,
        )
        self.db.add(edge)
        await self.db.commit()
        await self.db.refresh(edge)
        logger.info(f"EntityEdge stored: ({source_name}) -[{edge_type}]-> ({target_name}) conf={confidence}")
        return edge

    async def get_edges_for_entity(self, entity_name: str) -> list[EntityEdge]:
        """Get all active edges where the entity is source or target."""
        result = await self.db.execute(
            select(EntityEdge).where(
                EntityEdge.is_active == True,
                (
                    func.lower(EntityEdge.source_name) == entity_name.lower()
                ) | (
                    func.lower(EntityEdge.target_name) == entity_name.lower()
                )
            ).order_by(desc(EntityEdge.confidence))
        )
        return list(result.scalars().all())

    async def store_episode(
        self,
        title: str,
        summary: str,
        importance: float = 0.7,
        session_id: Optional[str] = None,
    ) -> Episode:
        """Store an important event or milestone."""
        episode = Episode(
            title=title,
            summary=summary,
            episode_type="conversation",
            related_session_id=session_id,
        )
        self.db.add(episode)
        await self.db.flush()

        if self.qdrant:
            try:
                vector = await embed_text(f"{title} {summary}")
                await self.qdrant.upsert(
                    collection_name="episodes",
                    points=[
                        qdrant_models.PointStruct(
                            id=episode.id,
                            vector=vector,
                            payload={
                                "title": title,
                                "summary": summary,
                                "importance": importance,
                            },
                        )
                    ],
                )
                episode.qdrant_id = episode.id
            except Exception as e:
                logger.warning(f"Qdrant upsert failed for episode: {e}")

        await self.db.commit()
        await self.db.refresh(episode)
        logger.info(f"Stored episode: {title}")
        return episode

    async def upsert_preference(
        self,
        key: str,
        value: str,
        evidence: Optional[str] = None,
        source: str = "inferred",
        confidence: float = 0.8,
    ) -> Preference:
        """Create or update a user preference."""
        result = await self.db.execute(
            select(Preference).where(Preference.key == key)
        )
        pref = result.scalar_one_or_none()

        if pref:
            pref.value = value
            pref.confidence = min(1.0, pref.confidence + 0.05)
            pref.occurrence_count += 1
            pref.updated_at = utc_now()
        else:
            pref = Preference(
                key=key,
                value=value,
                description=evidence,
                source=source,
                confidence=confidence,
            )
            self.db.add(pref)

        await self.db.commit()
        await self.db.refresh(pref)
        logger.info(f"Upserted preference [{key}]: {value}")
        return pref

    # ==============================================================
    # RECALL
    # ==============================================================

    async def search_semantic_memory(
        self, query: str, limit: int = 5
    ) -> list[SemanticMemory]:
        """Vector similarity search over semantic memories."""
        if not self.qdrant:
            # Fallback: return recent memories
            result = await self.db.execute(
                select(SemanticMemory)
                .where(SemanticMemory.is_active == True)
                .order_by(desc(SemanticMemory.created_at))
                .limit(limit)
            )
            return list(result.scalars().all())

        try:
            vector = await embed_text(query)
            search_result = await self.qdrant.search(
                collection_name="semantic_memory",
                query_vector=vector,
                limit=limit,
                score_threshold=0.5,
            )
            if not search_result:
                return []

            ids = [hit.id for hit in search_result]
            result = await self.db.execute(
                select(SemanticMemory).where(
                    SemanticMemory.id.in_(ids),
                    SemanticMemory.is_active == True,
                )
            )
            return list(result.scalars().all())
        except Exception as e:
            logger.warning(f"Semantic memory search failed: {e}")
            return []

    async def find_contact(self, name: str) -> list[Contact]:
        """Fuzzy contact search by name via Qdrant similarity."""
        if not self.qdrant:
            result = await self.db.execute(
                select(Contact).where(
                    Contact.is_active == True,
                    func.lower(Contact.name).contains(name.lower()),
                ).limit(5)
            )
            return list(result.scalars().all())

        try:
            vector = await embed_text(name)
            search_result = await self.qdrant.search(
                collection_name="contacts",
                query_vector=vector,
                limit=5,
                score_threshold=0.4,
            )
            if not search_result:
                return []

            ids = [hit.id for hit in search_result]
            result = await self.db.execute(
                select(Contact).where(
                    Contact.id.in_(ids),
                    Contact.is_active == True,
                )
            )
            return list(result.scalars().all())
        except Exception as e:
            logger.warning(f"Contact search failed: {e}")
            return []

    async def get_all_contacts(self) -> list[Contact]:
        result = await self.db.execute(
            select(Contact)
            .where(Contact.is_active == True)
            .order_by(desc(Contact.updated_at))
        )
        return list(result.scalars().all())

    async def get_all_active_facts(self) -> list[SemanticMemory]:
        result = await self.db.execute(
            select(SemanticMemory)
            .where(SemanticMemory.is_active == True)
            .order_by(desc(SemanticMemory.created_at))
        )
        return list(result.scalars().all())

    async def search_episodes(self, query: str, limit: int = 5) -> list[Episode]:
        """Vector similarity search for episodes."""
        if not self.qdrant:
            result = await self.db.execute(
                select(Episode).order_by(desc(Episode.created_at)).limit(limit)
            )
            return list(result.scalars().all())

        try:
            vector = await embed_text(query)
            search_result = await self.qdrant.search(
                collection_name="episodes",
                query_vector=vector,
                limit=limit,
                score_threshold=0.4,
            )
            ids = [hit.id for hit in search_result]
            if not ids:
                return []
            result = await self.db.execute(
                select(Episode).where(Episode.id.in_(ids))
            )
            return list(result.scalars().all())
        except Exception as e:
            logger.warning(f"Episode search failed: {e}")
            return []

    async def get_preferences(self) -> list[Preference]:
        result = await self.db.execute(
            select(Preference).order_by(desc(Preference.confidence))
        )
        return list(result.scalars().all())

    # ==============================================================
    # CONTEXT BUILDER — the most important method
    # ==============================================================

    def _scan_message_names(
        self, user_message: str, all_contacts: list
    ) -> tuple[list, list[dict]]:
        """
        Scan the message for contact names using n-grams (longest phrases
        first, so "jamil ali khan" resolves before "jamil" flags ambiguity).

        Returns (resolved_contacts, ambiguous_mentions):
          resolved_contacts  — Contact objects matched unambiguously
          ambiguous_mentions — [{"mention": str, "candidates": [{"id","name"}]}]
        Ambiguous candidates are NOT resolved entities — the caller must make
        the LLM ask, never assume.
        """
        words = re.findall(r"\b\w+\b", user_message.lower())
        if not words:
            return [], []

        resolved: list = []
        ambiguous: list[dict] = []
        matched_ids: set = set()
        consumed: set = set()

        for n in (3, 2, 1):
            for i in range(len(words) - n + 1):
                span = range(i, i + n)
                if any(j in consumed for j in span):
                    continue
                phrase = " ".join(words[i:i + n])
                if n == 1 and len(phrase) < 3:  # skip short/stop words
                    continue
                result = identify_contact(phrase, all_contacts)
                if result.status == ResolutionStatus.RESOLVED:
                    consumed.update(span)
                    if result.contact.id not in matched_ids:
                        matched_ids.add(result.contact.id)
                        resolved.append(result.contact)
                elif result.status == ResolutionStatus.AMBIGUOUS:
                    consumed.update(span)
                    ambiguous.append({"mention": phrase, "candidates": result.candidates})

        return resolved, ambiguous

    async def retrieve_context(
        self,
        user_message: str,
        session_id: Optional[str] = None,
        current_message: Optional[str] = None,
    ) -> RetrievedContext:
        """
        Search all memory types for content relevant to the user's message,
        and bundle it into a structured RetrievedContext.

        user_message may span recent turns (better retrieval recall);
        current_message, when given, is only the LATEST user message and is
        what gets scanned for name mentions — otherwise stale mentions from
        prior turns would re-flag ambiguity every turn.
        """
        # Read pending_resolution / pending_creation from the unified session
        pending_res_dict = None
        pending_creation_dict = None
        if session_id:
            sess = get_session(session_id)
            pending_res = sess.pending_resolution
            if pending_res is not None:
                if time.time() <= pending_res.expires:
                    pending_res_dict = {
                        "original_name": pending_res.original_name,
                        "candidates": pending_res.candidates,
                        "mentions": pending_res.mentions(),
                    }
                else:
                    # Expired — clear it
                    sess.pending_resolution = None
            pending_creation = sess.pending_creation
            if pending_creation is not None:
                if time.time() <= pending_creation.expires:
                    pending_creation_dict = {"name": pending_creation.name}
                else:
                    sess.pending_creation = None

        # 0. UserProfile (Phase 2.5)
        profile = await self.get_user_profile()

        # 1. Semantic memories
        memories = await self.search_semantic_memory(user_message, limit=5)

        # 2. Known people (lexical and semantic filtering).
        # Name-mention scanning runs on the CURRENT message only.
        all_contacts = await self.get_all_contacts()
        scan_text = current_message if current_message is not None else user_message
        lexical_contacts, ambiguous_mentions = self._scan_message_names(scan_text, all_contacts)
        semantic_contacts = await self.find_contact(user_message)

        # Merge lexical and semantic matches uniquely; ambiguous candidates are
        # included for display so the LLM can list them, but never as resolved.
        merged_map: dict = {c.id: c for c in lexical_contacts + semantic_contacts}
        for mention in ambiguous_mentions:
            for c_dict in mention["candidates"]:
                if c_dict["id"] not in merged_map:
                    c_obj = next((c for c in all_contacts if c.id == c_dict["id"]), None)
                    if c_obj:
                        merged_map[c_obj.id] = c_obj

        # Always inject active entities pinned in ConversationSession
        resolved_entities = []
        if session_id:
            sess = get_session(session_id)
            for entity in sess.active_entities:
                if entity.type == "contact":
                    c_obj = next((c for c in all_contacts if c.id == entity.id), None)
                    if c_obj and c_obj.id not in merged_map:
                        merged_map[c_obj.id] = c_obj

        # Populate resolved_entities from lexical contacts (deterministic resolution)
        for c in lexical_contacts:
            resolved_entities.append(ResolvedEntity(id=c.id, type="contact", name=c.name, confidence=100.0))

        # 3. Preferences
        prefs = await self.get_preferences()

        # 4. Relevant past episodes
        episodes = await self.search_episodes(user_message, limit=3)

        return RetrievedContext(
            resolved_entities=resolved_entities,
            pending_resolution=pending_res_dict,
            user_profile=profile,
            semantic_memories=memories,
            preferences=prefs,
            episodes=episodes,
            contacts=list(merged_map.values()),
            pending_creation=pending_creation_dict,
            ambiguous_mentions=ambiguous_mentions,
        )

    async def format_context(self, bundle: RetrievedContext) -> str:
        """
        Formats a RetrievedContext bundle into a string for the LLM prompt.
        """
        sections: list[str] = []

        if bundle.user_profile:
            profile_parts = []
            if bundle.user_profile.name:
                profile_parts.append(f"Name: {bundle.user_profile.name}")
            if bundle.user_profile.profession:
                profile_parts.append(f"Profession: {bundle.user_profile.profession}")
            if bundle.user_profile.location:
                profile_parts.append(f"Location: {bundle.user_profile.location}")
            if bundle.user_profile.background:
                profile_parts.append(f"Background: {bundle.user_profile.background}")
            if bundle.user_profile.work_style:
                profile_parts.append(f"Work style: {bundle.user_profile.work_style}")
            if bundle.user_profile.skills:
                try:
                    skills = json.loads(bundle.user_profile.skills)
                    if skills:
                        profile_parts.append(f"Skills: {', '.join(skills)}")
                except Exception:
                    pass
            if profile_parts:
                sections.append("WHO YOU ARE (stable identity - always use this):\n" + "\n".join(profile_parts))

        if bundle.semantic_memories:
            lines = [f"- {m.content}" for m in bundle.semantic_memories]
            sections.append("WHAT I KNOW ABOUT YOU:\n" + "\n".join(lines))

        if bundle.contacts:
            # Collect IDs of active/focus entities for marking
            active_ids: set[str] = set()
            focus_id: str | None = None
            if bundle.resolved_entities:
                for e in bundle.resolved_entities:
                    active_ids.add(e.id)
                if len(bundle.resolved_entities) == 1:
                    focus_id = bundle.resolved_entities[0].id

            people_lines = []
            for c in bundle.contacts:
                rel = c.relationship_type or "contact"
                last = f"last mentioned {c.last_interaction.strftime('%b %d')}" if c.last_interaction else "newly added"

                contact_details = []
                if c.birthday:
                    contact_details.append(f"Birthday: {c.birthday}")
                if c.email:
                    contact_details.append(f"Email: {c.email}")
                if c.phone:
                    contact_details.append(f"Phone: {c.phone}")
                if c.organization:
                    contact_details.append(f"Organization: {c.organization}")
                if c.skills:
                    try:
                        skills = json.loads(c.skills)
                        if skills:
                            contact_details.append(f"Skills: {', '.join(skills)}")
                    except Exception:
                        pass
                if c.summary:
                    contact_details.append(f"Summary: {c.summary}")
                if c.notes:
                    contact_details.append(f"Notes: {c.notes}")

                details_str = f" | {' | '.join(contact_details)}" if contact_details else ""

                # Mark the focus/active entity so the LLM never confuses similar names
                if c.id == focus_id:
                    marker = " [ACTIVE — the person we are currently discussing]"
                elif c.id in active_ids:
                    marker = " [ACTIVE]"
                else:
                    marker = ""

                line = f"- {c.name} ({rel.title()}){marker}{details_str} — {last}"
                
                # Fetch facts (interactions)
                result = await self.db.execute(
                    select(ContactInteraction)
                    .where(ContactInteraction.contact_id == c.id)
                    .order_by(ContactInteraction.interaction_date.asc())
                )
                facts = result.scalars().all()
                if facts:
                    # event_date is when it happened; interaction_date is only when it was recorded
                    fact_lines = [
                        f"    * [{f.category}] {f.description} (on {(f.event_date or f.interaction_date).strftime('%Y-%m-%d')})"
                        for f in facts
                    ]
                    line += "\n  Facts Log:\n" + "\n".join(fact_lines)
                
                people_lines.append(line)
            sections.append("PEOPLE YOU KNOW (Ask for clarification if user mentions a similar name or typo. The contact marked [ACTIVE] is the one currently being discussed — when the user says 'he', 'his', 'she', 'her', assume they mean this person):\n" + "\n".join(people_lines))

        if bundle.preferences:
            pref_lines = [f"- {p.value}" for p in bundle.preferences[:5]]
            sections.append("YOUR PREFERENCES:\n" + "\n".join(pref_lines))

        if bundle.episodes:
            ep_lines = []
            for ep in bundle.episodes:
                when = ep.created_at.strftime("%b %d")
                ep_lines.append(f"- {when}: {ep.title} -- {ep.summary[:120]}")
            sections.append("RELEVANT PAST CONTEXT:\n" + "\n".join(ep_lines))

        if not sections:
            return ""

        header = "=== MEMORY CONTEXT (use naturally, never mention this block) ==="
        footer = "=== END MEMORY CONTEXT ==="
        return "\n\n" + header + "\n\n" + "\n\n".join(sections) + "\n\n" + footer
