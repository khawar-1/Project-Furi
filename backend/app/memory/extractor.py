"""
Jarvis OS — Extraction Pipeline
After every user message, silently extract entities and preferences.
Runs as a background task — never blocks the streaming response.
"""
import json
import re
from datetime import datetime
from typing import Optional

from loguru import logger
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.engine import MemoryEngine
from app.memory.extraction_schema import ExtractionResult
from app.providers.base import LLMMessage, LLMProvider


ENTITY_EXTRACTION_PROMPT = """You are a silent memory extractor for a personal AI OS.
Your job is to analyze a FULL conversation and extract structured, accurate information.
The data you save will be read back by the AI in future sessions — it MUST be correct and self-contained.

CRITICAL DISAMBIGUATION RULES:
1. CLARIFICATION RESOLUTION: If the AI asked a clarification question (e.g. "Did you mean Jamil or Jamil Khan?") and the user replies with a relative reference like "the second one" or "Khan", you MUST output the FULL matched name (e.g. "Jamil Khan") in the `people_mentioned.name` field. DO NOT output "the second one".
2. EXTRACTION SCOPE: You MUST extract facts ONLY from the === CURRENT USER MESSAGE === below. The conversation history is provided SOLELY to help you resolve pronouns ("he", "it"), vague references ("the second one"), and corrections ("I meant Hamil"). DO NOT re-extract or re-save facts that appeared only in previous turns and are not repeated in the current message.
3. RELATIVE DATES: ALWAYS convert relative time references ("today", "yesterday", "this morning", "last Friday", "two days ago") to an ABSOLUTE date in YYYY-MM-DD format using the CURRENT SYSTEM DATE below. NEVER write relative time words into a saved fact. If a fact refers to the future or the date genuinely cannot be resolved to an absolute date, omit the date rather than guessing.

=== CURRENT SYSTEM DATE ===
{current_date}

=== USER IDENTITY ===
The user's name is: {user_name}
(When you write a fact from a contact's perspective, refer to the user with the literal placeholder {{USER}} — never their actual name; the system substitutes it.)

=== FULL CONVERSATION (use this to resolve ALL pronouns and vague references) ===
{conversation}

=== CURRENT USER MESSAGE ===
{message}

=== EXISTING MEMORY ===
Contacts already saved: {existing_contacts}
Facts already saved: {existing_facts}

Return ONLY valid JSON with this exact structure (no explanation, no markdown):
{{
  "people_mentioned": [
    {{
      "name": "The name EXACTLY as the user wrote it in the CURRENT USER MESSAGE ('jamil' stays 'jamil' — do NOT expand it to a fuller saved contact name). Only exception: an explicit clarification per DISAMBIGUATION RULE 1.",
      "email": "null or string",
      "phone": "null or string",
      "relationship": "friend|colleague|client|family|recruiter|mentor|other",
      "skills": ["Python", "AI Engineering"],
      "birthday": "null or YYYY-MM-DD or MM-DD. If the user does not explicitly state the birth year, you MUST use MM-DD format (e.g. '07-03'). DO NOT append the current year just because the current system date is provided.",
      "important_dates": {{"anniversary": "YYYY-MM-DD"}},
      "new_facts": [
        {{
          "fact": "A standalone factual statement about THIS PERSON ONLY, not involving the user (e.g. 'Got a new job at Google as Senior Developer'). Facts involving the user go in facts_about_user instead — NEVER duplicate them here. If the user must be referenced, use the literal placeholder {{USER}}, never their name and never 'you'.",
          "category": "work|personal|contact|history|other"
        }}
      ]
    }}
  ],
  "contacts_to_delete": ["List of contact names that were created by mistake and the user is now correcting (e.g. they misspelled a name and are correcting it)"],
  "relationships": [
    {{
      "source_name": "Ali Raza",
      "source_type": "contact",
      "edge_type": "COLLABORATES_WITH",
      "target_name": "Sara Khan",
      "target_type": "contact",
      "confidence": 1.0,
      "edge_label": null
    }}
  ],
  "user_profile_enrichment": {{
    "name": "null or string",
    "profession": "null or string",
    "location": "null or string",
    "background": "null or string",
    "work_style": "null or string",
    "skills": ["skill1", "skill2"],
    "languages": ["language1"]
  }},
  "facts_about_user": [
    {{
      "fact_user_perspective": "The fact written for the USER's own memory. Refer to any involved contact with the placeholder {{CONTACT:name-exactly-as-the-user-said-it}}. Example: 'Went to coffee with {{CONTACT:Ali}} on 2026-07-06'.",
      "fact_contact_perspective": "The SAME fact rewritten for the CONTACT's fact log, referring to the user as {{USER}}. Example: 'Went to coffee with {{USER}} on 2026-07-06'. Empty string if subject is 'user'.",
      "subject": "user | shared",
      "related_contacts": ["Name exactly as the user said it, e.g. 'Ali'"],
      "event_date": "YYYY-MM-DD if the fact is tied to a specific resolvable date, else null"
    }}
  ],
  "facts_to_supersede": [
    "Exact text of existing facts from memory that are now outdated, duplicated, or merged into a new fact"
  ],
  "important_events": [
    {{"title": "string", "summary": "string", "importance": 0.8}}
  ],
  "preferences": [
    {{"key": "unique_key_snake_case", "value": "The preference description", "evidence": "what triggered this"}}
  ]
}}

CRITICAL RULES — READ CAREFULLY:
1. USE CONVERSATION CONTEXT: Resolve ALL pronouns. If the user says "it's called Remo Office", look back in the conversation to see what "it" refers to. Never leave a reference unresolved.
2. CORRECTIONS: If the user corrects a name or entity (e.g. "I meant Hamil" instead of "Jameell"), you MUST re-extract any facts (like phone numbers, emails, skills) that were provided for the wrong name in recent turns and attach them to the correct name in the current extraction.
3. CONTACTS — HUMANS ONLY: NEVER create contact entries for AI tools, bots, or software. This includes: Jarvis, ChatGPT, GPT-4, Claude, Gemini, Antigravity, Copilot, Llama, Mistral. NEVER include the USER THEMSELVES in people_mentioned — the user is not their own contact; facts about the user go in user_profile_enrichment or facts_about_user.
4. DATES IN FACTS: If a fact involves a specific event, date, or time, resolve the relative date using the CURRENT SYSTEM DATE above, include the absolute YYYY-MM-DD date inside the fact text itself, AND set "event_date".
5. SKILLS: Extract skills for people (e.g. "Python", "AI Engineering", "React"). Always populate the "skills" array when a person's skills are mentioned.
6. RELATIONSHIPS: Extract every explicit relationship as an edge. Use ONLY these edge_types: FRIEND_OF, CLIENT_OF, COLLABORATES_WITH. For anything else, use OTHER and populate edge_label. Confidence: 1.0 if user explicitly stated it, 0.7 if inferred.
7. USER PROFILE: Use "user_profile_enrichment" for STABLE facts about the user themselves (name, profession, skills, background). Use "facts_about_user" ONLY for TEMPORARY or CONTEXTUAL facts.
8. FACTS SUBJECT AND PERSPECTIVES: For each entry in "facts_about_user":
   - Set subject to "user" if the fact is purely about the user. fact_contact_perspective must be "" and related_contacts [].
   - Set subject to "shared" if the fact involves the user AND one or more other people (shared activities, meetings, conversations, plans). List each person in "related_contacts" EXACTLY as the user said their name — do NOT expand "Ali" to a full contact name yourself, even if similar contacts exist. The system resolves identities.
   - In fact_user_perspective, wrap every person's name in {{CONTACT:...}} using the name as said. In fact_contact_perspective, refer to the user only as {{USER}}.
9. AMBIGUOUS NAMES & TYPOS: If the user mentions a person whose name is highly similar to an EXISTING contact, DO NOT autocorrect or expand it. The ONLY time you resolve a similar name is if the user has EXPLICITLY clarified it in the conversation history.
10. FACTS SUPERSEDING AND MERGING: If a new fact relates to an existing fact, combine them into a single comprehensive fact. Add the EXACT text of ALL old overlapping facts to "facts_to_supersede".
11. PREFERENCES: Detect when the user explicitly states "I prefer...", "I like...", "I hate...", or corrects your style. Return in "preferences".
12. Return empty arrays [] or null values for categories with nothing to extract.
"""


def _format_conversation(conversation_history: list[dict]) -> str:
    """Format conversation history for the extraction prompt."""
    if not conversation_history:
        return "(no prior conversation)"
    lines = []
    for msg in conversation_history[-8:]:  # Last 8 messages for context (reduced from 12 to save tokens)
        role = msg.get("role", "user").upper()
        content = msg.get("content", "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "(no prior conversation)"


def _parse_extraction_json(content: str) -> ExtractionResult:
    """Strip code fences, parse JSON, and validate against the schema."""
    content = content.strip()
    content = re.sub(r"^```(?:json)?\n?", "", content)
    content = re.sub(r"\n?```$", "", content)
    return ExtractionResult.model_validate(json.loads(content))


async def extract_entities(
    message: str,
    provider: LLMProvider,
    existing_contacts: list[str],
    existing_facts: list[str],
    conversation_history: list[dict],
    user_name: Optional[str] = None,
) -> Optional[ExtractionResult]:
    """
    Use the LLM to extract structured entities from a user message.
    Output is validated against ExtractionResult; one retry with the
    validation error appended, then give up (extraction is best-effort).
    """
    conversation_str = _format_conversation(conversation_history)
    # Local time, not UTC: "this morning" must resolve to the user's calendar
    # day. Weekday included so "last Friday" is resolvable.
    now = datetime.now()
    current_date = f"{now.strftime('%Y-%m-%d')} ({now.strftime('%A')})"
    prompt = ENTITY_EXTRACTION_PROMPT.format(
        current_date=current_date,
        user_name=user_name or "(not yet known)",
        conversation=conversation_str,
        message=message,
        existing_contacts=", ".join(existing_contacts) if existing_contacts else "none",
        existing_facts="\n- ".join([""] + existing_facts) if existing_facts else "none",
    )

    messages = [LLMMessage(role="user", content=prompt)]
    for attempt in (1, 2):
        try:
            response = await provider.chat(
                messages=messages,
                temperature=0.0,
                max_tokens=1500,
            )
        except Exception as e:
            logger.warning(f"Entity extraction LLM call failed (attempt {attempt}): {e}")
            return None
        try:
            return _parse_extraction_json(response.content)
        except (json.JSONDecodeError, ValidationError) as e:
            logger.warning(f"Entity extraction output invalid (attempt {attempt}): {e}")
            if attempt == 1:
                # Feed the error back once so the model can correct itself
                messages = messages + [
                    LLMMessage(role="assistant", content=response.content),
                    LLMMessage(
                        role="user",
                        content=(
                            "Your previous output was not valid against the required JSON "
                            f"schema: {e}\nReturn ONLY the corrected valid JSON, nothing else."
                        ),
                    ),
                ]
    return None


# Names that must never become contacts, even if the LLM slips
AI_TOOLS = {
    "jarvis", "chatgpt", "gpt", "gpt-4", "gpt-3", "claude", "gemini",
    "antigravity", "copilot", "llama", "mistral", "bard", "perplexity",
    "midjourney", "dall-e", "stable diffusion", "anthropic", "openai",
}

# Casual messages that are definitely not memory-worthy
SKIP_WORDS = {
    "ok", "okay", "thanks", "thank you", "lol", "yes", "no", "sure",
    "haha", "hi", "hey", "hello", "yep", "nope", "cool",
}


async def run_extraction_pipeline(
    user_message: str,
    assistant_message: str,
    session_id: Optional[str],
    db: AsyncSession,
    engine: MemoryEngine,
    provider: LLMProvider,
    conversation_history: list[dict] | None = None,
) -> None:
    """
    Full extraction pipeline — runs silently after every chat turn.
    Extracts entities (people, facts, events) AND preferences.
    All failures are caught and logged — never surfaces to the user.
    """
    try:
        conversation_history = conversation_history or []

        # Inject provider into engine for intelligent note merging
        engine.provider = provider

        # FAST CLASSIFIER: Skip extraction entirely if message is definitely not memory-worthy.
        # This saves thousands of tokens per casual message ("thanks", "ok", "lol").
        msg_lower = user_message.lower().strip()
        words = msg_lower.split()
        if len(words) < 5:
            if msg_lower in SKIP_WORDS or (len(words) == 1 and words[0] in SKIP_WORDS):
                logger.info(f"Skipping extraction for non-memory message: '{user_message}'")
                return

        # Fetch existing data for disambiguation and deduplication
        all_contacts = await engine.get_all_contacts()
        existing_contacts = [f"{c.name} ({c.organization or 'No Org'})" for c in all_contacts]

        # Cap facts to the 15 most recent to prevent context bloat over time
        active_facts_objs = await engine.get_all_active_facts()
        existing_facts = [f.content for f in active_facts_objs[-15:]]

        user_name = await engine.get_user_name()

        # Run extraction (merged entities + preferences), schema-validated
        entities = await extract_entities(
            user_message, provider,
            existing_contacts, existing_facts,
            conversation_history,
            user_name=user_name,
        )
        if entities is None:
            logger.warning("Extraction skipped: no valid output from the LLM")
            return

        # --- Supersede outdated/vague facts
        for old_fact in entities.facts_to_supersede:
            await engine.delete_semantic_memory_by_content(old_fact)

        # --- Store people (humans only, and never the user themselves)
        from app.memory.engine import normalize_name
        # The user's name may only be arriving in THIS extraction
        # ("My name is Khawar") — check the enrichment too, not just the profile.
        enriched_name = (
            entities.user_profile_enrichment.name
            if entities.user_profile_enrichment else None
        )
        user_name_norms = {
            normalize_name(n) for n in (user_name, enriched_name) if n
        }
        for person in entities.people_mentioned:
            name = person.name
            if not name or len(name) < 2:
                continue
            # Block AI tools from being saved as contacts
            if name.lower() in AI_TOOLS or any(ai in name.lower() for ai in ["gpt", "llm", "ai ", " ai"]):
                logger.debug(f"Skipping AI tool contact: {name}")
                continue
            # The user is not their own contact — the LLM sometimes slips this in
            if normalize_name(name) in user_name_norms:
                logger.debug(f"Skipping user-as-contact: {name}")
                continue

            await engine.store_contact(name, {
                "email": person.email,
                "phone": person.phone,
                "relationship_type": person.relationship,
                "skills": person.skills,
                "birthday": person.birthday,
                "important_dates": person.important_dates,
                "new_facts": [f.model_dump() for f in person.new_facts],
            }, session_id=session_id)

        # --- Delete mistakenly created contacts
        for deleted_contact in entities.contacts_to_delete:
            await engine.delete_contact_by_name(deleted_contact)

        # --- Process user_profile_enrichment
        if entities.user_profile_enrichment:
            clean = entities.user_profile_enrichment.non_empty_fields()
            if clean:
                await engine.store_user_profile(clean)
                logger.info(f"UserProfile enriched with: {list(clean.keys())}")

        # --- Process typed relationship edges
        VALID_EDGE_TYPES = {
            "WORKS_ON", "COLLABORATES_WITH", "REPORTS_TO", "MANAGES",
            "FRIEND_OF", "CLIENT_OF", "HIRED_BY", "HAS_SKILL", "USES_TOOL", "OTHER",
        }
        for rel in entities.relationships:
            edge_type = rel.edge_type.upper()
            if edge_type not in VALID_EDGE_TYPES:
                logger.debug(f"Skipping unknown edge_type: {edge_type}")
                continue
            if not rel.source_name or not rel.target_name:
                continue
            await engine.store_entity_edge(
                source_type=rel.source_type,
                source_name=rel.source_name,
                edge_type=edge_type,
                target_type=rel.target_type,
                target_name=rel.target_name,
                confidence=rel.confidence,
                edge_label=rel.edge_label,
                session_id=session_id,
            )

        # --- Facts about the user: dual-perspective, identity-resolution routed.
        # RESOLVED contacts get both writes immediately; AMBIGUOUS parks the
        # fact behind a "which X?" question; NOT_FOUND parks it behind a
        # "create X?" question. See MemoryEngine.store_shared_fact.
        for fact in entities.facts_about_user:
            text = fact.fact_user_perspective
            if not text or len(text) < 10:
                continue

            category = "fact"
            fact_lower = text.lower()
            if any(w in fact_lower for w in ["prefer", "like", "love", "hate", "dislike"]):
                category = "preference"
            elif any(w in fact_lower for w in ["skill", "know", "expert", "experience"]):
                category = "skill"
            elif any(w in fact_lower for w in ["work", "job", "role", "engineer", "developer",
                                               "building", "working on"]):
                category = "work"

            fact_dict = fact.model_dump()
            fact_dict["category"] = category
            await engine.store_shared_fact(fact_dict, session_id=session_id)

        # --- Store important events
        for event in entities.important_events:
            if event.importance >= 0.6:
                await engine.store_episode(
                    title=event.title or "Notable Event",
                    summary=event.summary,
                    importance=event.importance,
                    session_id=session_id,
                )

        # --- Store preferences (extracted in the same pass)
        for pref in entities.preferences:
            if pref.key and pref.value:
                await engine.upsert_preference(
                    key=pref.key,
                    value=pref.value,
                    evidence=pref.evidence,
                    source="inferred",
                )

        logger.debug("Extraction pipeline completed successfully")

    except Exception as e:
        # Never let extraction errors surface to the user
        logger.warning(f"Extraction pipeline error (non-critical): {e}")
