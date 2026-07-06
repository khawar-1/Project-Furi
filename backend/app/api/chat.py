"""
Jarvis OS — Chat API (Phase 2)
Now with memory context injection and background extraction pipeline.
"""
import asyncio
import json
import re
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db, get_llm_provider, get_qdrant
from app.db.models import Message
from app.db.schemas import ChatRequest, ChatResponse, StreamChunk
from app.memory.engine import MemoryEngine, substitute_placeholders
from app.memory.extractor import run_extraction_pipeline
from app.providers.base import LLMMessage, LLMProvider

router = APIRouter()

# Deterministic yes/no reading of the user's reply to "want me to add them?"
_NEGATIVE_RE = re.compile(r"\b(no|nope|nah|don'?t|do not|not now|skip|leave it)\b")
_AFFIRMATIVE_RE = re.compile(r"\b(yes|yeah|yep|sure|ok|okay|of course|go ahead|do it|add (her|him|them|it))\b")


def _interpret_yes_no(text: str) -> Optional[bool]:
    """True/False for a clear answer, None when the reply is something else."""
    t = text.strip().lower()
    if _NEGATIVE_RE.search(t):
        return False
    if _AFFIRMATIVE_RE.search(t):
        return True
    return None


def _saved_just_now_clause(saved_texts: list) -> str:
    """
    Spell out exactly what was written this turn. The parked writes land in
    the DB BEFORE format_context renders the memory block, so the just-saved
    fact already shows up in MEMORY CONTEXT of the same prompt — without this
    clause the LLM reads it as an old memory ("we had previously noted...").
    """
    texts = [t for t in saved_texts if t]
    if not texts:
        return ""
    listed = "; ".join(f'"{t}"' for t in texts)
    return (
        f" SAVED JUST NOW (this very moment, from the user's current confirmation): {listed}. "
        f"If this same information appears in the MEMORY CONTEXT above, that entry was written seconds ago — "
        f"it is NOT something you knew before and NOT a previous plan. The user is telling you this for the FIRST time."
    )


def _build_system_prompt(
    memory_context: str = "",
    pending_resolution: dict = None,
    disambiguation_resolved_note: str = None,
    pending_creation: dict = None,
    ambiguous_mentions: list = None,
) -> str:
    """Build the Jarvis OS system prompt, with optional memory context block."""
    from datetime import datetime
    current_datetime = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    
    base = f"""You are Jarvis, a personal AI operating system — a digital extension of the user, not a generic assistant.

IDENTITY:
- You are calm, precise, and intelligent. You speak like a trusted senior colleague.
- You are proactive but never presumptuous.
- You are direct. No filler phrases like "Certainly!", "Of course!", "Great question!".
- Never refer to yourself as an AI, assistant, or chatbot. You are Jarvis.

MEMORY RULES:
- Everything in the MEMORY CONTEXT below is confirmed fact. Use it naturally in responses.
- When a contact has multiple facts in the same category with different values, the most recent fact is the current truth. Older facts are historical context only.
- Never say "according to my memory" or "I remember that". Just use the knowledge naturally.
- If the memory context is empty, you know nothing about the user yet. Ask to learn.
- Never fabricate facts, past interactions, emails, reminders, or events not in memory.
- If the user tells you something new about themselves, acknowledge it naturally.
- TIMELINE HONESTY: memory entries may have been written seconds ago from THIS very conversation. Never claim the user told you something "previously" or that something "was already in our plans" unless you are certain it came from an earlier conversation — when in doubt, treat it as new information the user just gave you.
- Current date and time: {current_datetime}

CLARIFICATION RULES:
- Never assume the identity of vague references ("he", "this project", "that file").
- AMBIGUITY DETECTION (this is mandatory, not optional): Before acting on ANY contact name, scan ALL the names in the PEOPLE YOU KNOW section. If there are 2 or more entries whose names contain the same word (e.g. "jamil" appears in "jamil", "jami", "Jamil Ali", "jamil ali khan"), they are ALL considered ambiguous — even if one is an exact character match. You MUST ask the user which specific person they mean BEFORE proceeding. Do not assume the exact match is correct. Do not act on any information until the user explicitly picks one.
- Once the user explicitly names which person (e.g. "I meant Jamil Ali" or "the second one"), accept it immediately and proceed. Do NOT re-question or hedge.
- If the user asks to save information for a person, check if that exact name OR a very similar spelling exists in the MEMORY CONTEXT. If there is a clear match, use it naturally. If there is NO reasonably close match, inform the user the person is not in your contacts.
- If a project is mentioned without an explicit name, confirm which project before acting.
- If someone is described as helping/working without naming the project, ask which project.
- One clarifying question at a time. Never interrogate the user.

GROUNDING RULE:
- If the user uses a pronoun ("he", "she", "his", "her", "they", "it") and the active entity/topic is shown in the memory context, treat that pronoun as referring to that active entity. If there is NO active entity in context and the referent is unclear, you MUST ask "Who are you referring to?" — never guess or fabricate.

MEMORY HONESTY RULE:
- NEVER say "I've noted", "I've saved", "I've updated", "I'll remember", "I've added", or any similar confirmation that implies you stored something.
- You do NOT have direct control over memory. Memory is written by a separate background system after your response.
- When the user tells you a fact, simply acknowledge it naturally in conversation (e.g. "Got it" or "That's August 6th then."). Do NOT claim you stored it.
- If the user asks you to save something, respond with what you understood (e.g. "Jamil Ali's birthday is August 6th — noted.") but do not claim it was written.
- The ONLY exception: when a [SYSTEM NOTE — BACKEND RESOLVED] below explicitly states that something WAS saved or created, the backend has already written it — you may (and should) confirm that naturally.

RESPONSE STYLE:
- Be concise by default. Expand only when the topic requires depth.
- Use plain language. Avoid jargon unless the user uses it first.
- Never add unnecessary caveats or disclaimers.
- Format with markdown only when it genuinely helps readability.
"""

    if memory_context:
        base += f"""
{memory_context}

Use the above context naturally. Do not recite it back. Do not reference it explicitly.
Simply let it inform how you respond, as a person would use their own memory.
"""

    # Disambiguation resolved — inject clear directive instead of the pending block
    if disambiguation_resolved_note:
        base += f"""
[SYSTEM NOTE — BACKEND RESOLVED]: {disambiguation_resolved_note}
"""
    elif pending_resolution:
        mentions = pending_resolution.get("mentions") or [{
            "name": pending_resolution["original_name"],
            "candidates": pending_resolution["candidates"],
        }]
        mention_lines = [
            f'- "{m["name"]}" → possible matches: {", ".join(c["name"] for c in m["candidates"])}'
            for m in mentions
        ]
        example = " And ".join(
            f'by \'{m["name"]}\' did you mean {" or ".join(c["name"] for c in m["candidates"][:3])}?'
            for m in mentions[:2]
        )
        base += f"""
PENDING DISAMBIGUATION:
A background process flagged the following name(s) from the user's earlier message as ambiguous:
{chr(10).join(mention_lines)}
However, check the latest user message first:
- If the user has ALREADY clearly stated which person they mean for EVERY name above, accept it and proceed — do NOT ask again.
- Otherwise, ask ONE short question that handles EACH unresolved name SEPARATELY, listing only that name's own options (e.g. "Quick check — {example}"). NEVER merge different names' options into combined guesses, and do not invent alternatives.
"""
    elif pending_creation:
        base += f"""
PENDING CONTACT CREATION:
The user mentioned "{pending_creation['name']}", who is NOT in their contacts. Information about this person is on hold.
Ask the user exactly one question: "{pending_creation['name']} isn't in your contacts — want me to add them?"
Do not save or assume anything about this person until the user answers. If the user's latest message already answers this (yes/no), acknowledge and move on — do not ask again.
"""
    elif ambiguous_mentions:
        mention_lines = []
        for m in ambiguous_mentions:
            names = ", ".join(c["name"] for c in m["candidates"])
            mention_lines.append(f'- "{m["mention"]}" could be any of: {names}')
        example = " And ".join(
            f'by \'{m["mention"]}\' did you mean {" or ".join(c["name"] for c in m["candidates"][:3])}?'
            for m in ambiguous_mentions[:2]
        )
        base += f"""
AMBIGUOUS NAME(S) IN THE CURRENT MESSAGE (backend-verified — this is mandatory):
{chr(10).join(mention_lines)}
The user's latest message names one or more people who each match MULTIPLE saved contacts. You MUST ask which one they mean before confirming, acting on, or discussing any information about those people. Do NOT assume — not even the exact-spelling match. Nothing has been saved yet; the information is held until the user answers.
Ask exactly ONE short question, but handle EACH ambiguous name SEPARATELY inside it: for every name above, list only that name's own options (e.g. "Quick check — {example}"). NEVER merge different names' options into combined guesses like "is it X and one of Y or Z".
"""

    return base


async def _persist_message(
    db: AsyncSession,
    session_id: str,
    role: str,
    content: str,
    model: Optional[str] = None,
    tokens_used: Optional[int] = None,
) -> None:
    """Save a message to the SQLite messages table."""
    msg = Message(
        session_id=session_id,
        role=role,
        content=content,
        model=model,
        tokens_used=tokens_used,
    )
    db.add(msg)
    await db.commit()


@router.post("/stream", summary="Streaming chat completion (SSE)")
async def chat_stream(
    request: ChatRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
    qdrant=Depends(get_qdrant),
) -> StreamingResponse:
    """
    Stream a chat completion response as Server-Sent Events.
    Phase 2: Injects memory context into system prompt.
    Fires background extraction after streaming completes.
    """
    session_id = request.session_id or str(uuid.uuid4())

    # --- Phase 2: Build memory context and update state
    from app.memory.conversation_state import get_session, touch_session, ActiveEntity
    import time
    
    memory_engine = MemoryEngine(db=db, qdrant=qdrant)
    # Get the last 3 user messages to provide better context for short replies (like "yes")
    user_msgs = [m.content for m in request.messages if m.role == "user"]
    recent_user_text = "\n".join(user_msgs[-3:])
    
    memory_context = ""
    pending_resolution = None
    pending_creation = None
    ambiguous_mentions = []
    disambiguation_resolved_note = None  # Injected into system prompt when resolved

    if recent_user_text:
        try:
            bundle = await memory_engine.retrieve_context(
                recent_user_text,
                session_id=session_id,
                current_message=user_msgs[-1] if user_msgs else None,
            )

            # --- DETERMINISTIC DISAMBIGUATION RESOLUTION ---
            # If there's an active pending_resolution in the session, try to resolve the
            # user's latest message against the candidates IN PYTHON — before calling the LLM.
            # This prevents the LLM from asking the same question twice.
            sess = get_session(session_id)
            last_user_msg_content = user_msgs[-1] if user_msgs else ""
            
            if sess.pending_resolution is not None and last_user_msg_content:
                from app.memory.engine import resolve_confirmation_multi
                pr = sess.pending_resolution
                all_contacts = await memory_engine.get_all_contacts()
                contacts_by_id = {c.id: c for c in all_contacts}
                mentions = pr.mentions()

                # One reply may settle SEVERAL pending names at once
                # ("i meant hamil and ali raza" answering both "ali" and "jamil").
                assignment = resolve_confirmation_multi(last_user_msg_content, mentions, all_contacts)

                if not assignment:
                    sess.pending_resolution = None
                    touch_session(session_id)
                    disambiguation_resolved_note = (
                        f"DISAMBIGUATION FAILED: The user replied '{last_user_msg_content}' which did not match "
                        f"any known contacts. Tell the user exactly this: 'This contact isn't in your contact list. "
                        f"If you want me to remember facts about them, please save them as a contact first.'"
                    )
                else:
                    # Confirmed contacts: names settled in prior turns plus this reply.
                    preresolved = {
                        as_said: contacts_by_id[cid]
                        for as_said, cid in pr.resolved_so_far.items()
                        if cid in contacts_by_id
                    }
                    for as_said, cid in assignment.items():
                        if cid in contacts_by_id:
                            preresolved[as_said.lower()] = contacts_by_id[cid]

                    resolved_contacts = [
                        contacts_by_id[cid] for cid in assignment.values() if cid in contacts_by_id
                    ]
                    primary = preresolved.get(pr.original_name.lower()) or (
                        resolved_contacts[0] if resolved_contacts else None
                    )
                    if resolved_contacts:
                        entities = [
                            ActiveEntity(
                                id=c.id, type="contact", name=c.name,
                                confidence=1.0, last_mentioned=time.time(),
                            )
                            for c in resolved_contacts
                        ]
                        sess.active_entities = entities
                        sess.focus_entity = entities[0] if len(entities) == 1 else None

                    # Apply the parked writes deterministically. Facts are
                    # re-routed through store_shared_fact with the confirmed
                    # contacts pinned: fully-resolved facts write both
                    # perspectives; facts with names STILL unresolved re-park
                    # and the question below asks only about those.
                    parked_facts = pr.pending_shared_facts
                    pending_update = pr.pending_update
                    sess.pending_resolution = None  # clear before re-routing; re-parking recreates it
                    touch_session(session_id)

                    saved_something = False
                    saved_texts: list = []
                    try:
                        if pending_update and primary:
                            await memory_engine.update_contact(primary.id, pending_update)
                            saved_something = True
                            user_name_note = await memory_engine.get_user_name()
                            saved_texts.extend(
                                substitute_placeholders(
                                    f.get("fact", ""), user_name=user_name_note,
                                    default_contact_name=primary.name,
                                )
                                for f in pending_update.get("new_facts", []) if f.get("fact")
                            )
                        for parked_fact in parked_facts:
                            text = await memory_engine.store_shared_fact(
                                parked_fact, session_id=session_id, preresolved=preresolved
                            )
                            if text:
                                saved_texts.append(text)
                                saved_something = True
                    except Exception as e:
                        logger.warning(f"Applying parked writes after disambiguation failed: {e}")

                    resolved_names = ", ".join(dict.fromkeys(c.name for c in resolved_contacts))
                    saved_note = (
                        f"The pending information from their previous message has now been successfully linked and saved to: {resolved_names}. "
                        if saved_something else
                        f"The original pending fact is now securely linked to: {resolved_names}. "
                    )
                    disambiguation_resolved_note = (
                        f"DISAMBIGUATION RESOLVED: The user meant: {resolved_names}. "
                        + saved_note +
                        f"CRITICAL: Acknowledge this naturally (e.g., 'Got it. Noted.'), but DO NOT phrase your response as if this is an old memory you just remembered (e.g. do NOT say 'we had previously noted' or 'that's already in our plans'). The user literally just told you this in the previous turn! Do NOT ask for clarification again."
                        + _saved_just_now_clause(saved_texts)
                    )
                    # If some names are STILL unresolved, the fact re-parked —
                    # tell the LLM to ask about those names only.
                    still_pending = get_session(session_id).pending_resolution
                    if still_pending is not None:
                        remaining = "; ".join(
                            f'"{m["name"]}" (options: {", ".join(c["name"] for c in m["candidates"])})'
                            for m in still_pending.mentions()
                        )
                        disambiguation_resolved_note += (
                            f" HOWEVER, the fact also involves name(s) still ambiguous: {remaining}. "
                            f"After acknowledging, ask ONE short question — for EACH remaining name separately, "
                            f"list that name's options and ask which one they meant."
                        )

            # --- PENDING CONTACT CREATION: resolve the yes/no in Python ---
            elif sess.pending_creation is not None and last_user_msg_content:
                pc = sess.pending_creation
                if time.time() > pc.expires:
                    sess.pending_creation = None
                else:
                    # Sanity recheck: never ask to create a name that actually
                    # resolves against the contact list (the extractor may have
                    # normalized/expanded a name it saw in memory context).
                    from app.memory.engine import identify_contact, ResolutionStatus
                    from app.memory.conversation_state import PendingResolution
                    all_contacts_chk = await memory_engine.get_all_contacts()
                    chk = identify_contact(pc.name, all_contacts_chk)
                    if chk.status == ResolutionStatus.RESOLVED:
                        parked_facts = pc.pending_shared_facts
                        all_contacts_map = {c.id: c for c in all_contacts_chk}
                        preresolved = {
                            as_said: all_contacts_map[cid]
                            for as_said, cid in pc.resolved_so_far.items()
                            if cid in all_contacts_map
                        }
                        preresolved[pc.name.lower()] = chk.contact
                        sess.pending_creation = None  # clear before re-routing
                        touch_session(session_id)
                        try:
                            saved_any = False
                            saved_texts = []
                            if any(v for v in pc.pending_update.values()):
                                await memory_engine.update_contact(chk.contact.id, pc.pending_update)
                                saved_any = True
                                user_name_note = await memory_engine.get_user_name()
                                saved_texts.extend(
                                    substitute_placeholders(
                                        f.get("fact", ""), user_name=user_name_note,
                                        default_contact_name=chk.contact.name,
                                    )
                                    for f in pc.pending_update.get("new_facts", []) if f.get("fact")
                                )
                            for parked_fact in parked_facts:
                                text = await memory_engine.store_shared_fact(
                                    parked_fact, session_id=session_id, preresolved=preresolved
                                )
                                if text:
                                    saved_texts.append(text)
                                    saved_any = True
                            if saved_any:
                                disambiguation_resolved_note = (
                                    f"NAME MATCHED EXISTING CONTACT: \"{pc.name}\" is the saved contact "
                                    f"\"{chk.contact.name}\" — they were NOT missing. The pending information "
                                    f"from their previous message has now been successfully saved to \"{chk.contact.name}\". "
                                    f"CRITICAL: Acknowledge this naturally without sounding like it is an old memory (e.g. do NOT say 'we had previously noted'). Do NOT offer to create a contact."
                                    + _saved_just_now_clause(saved_texts)
                                )
                        except Exception as e:
                            logger.warning(f"Applying rechecked pending creation failed: {e}")
                        if sess.pending_creation is pc:  # re-routing may have parked a NEW question
                            sess.pending_creation = None
                        touch_session(session_id)
                    elif chk.status == ResolutionStatus.AMBIGUOUS:
                        # Multiple matches now — convert into a disambiguation question
                        if sess.pending_resolution is None:
                            sess.pending_resolution = PendingResolution(
                                original_name=pc.name,
                                pending_update=pc.pending_update,
                                candidates=chk.candidates,
                                pending_shared_facts=pc.pending_shared_facts,
                                unresolved_mentions=[{"name": pc.name, "candidates": chk.candidates}],
                                resolved_so_far=dict(pc.resolved_so_far),
                            )
                        sess.pending_creation = None
                        touch_session(session_id)
                    else:
                        answer = _interpret_yes_no(last_user_msg_content)
                        if answer is True:
                            try:
                                new_contact = await memory_engine.create_contact_manual(
                                    pc.name,
                                    {k: v for k, v in pc.pending_update.items() if k != "new_facts"},
                                )
                                parked_facts = pc.pending_shared_facts
                                all_contacts_map = {c.id: c for c in all_contacts_chk}
                                preresolved = {
                                    as_said: all_contacts_map[cid]
                                    for as_said, cid in pc.resolved_so_far.items()
                                    if cid in all_contacts_map
                                }
                                preresolved[pc.name.lower()] = new_contact
                                sess.pending_creation = None  # clear before re-routing
                                touch_session(session_id)
                                saved_texts = []
                                if pc.pending_update.get("new_facts"):
                                    await memory_engine.update_contact(
                                        new_contact.id, {"new_facts": pc.pending_update["new_facts"]}
                                    )
                                    user_name_note = await memory_engine.get_user_name()
                                    saved_texts.extend(
                                        substitute_placeholders(
                                            f.get("fact", ""), user_name=user_name_note,
                                            default_contact_name=new_contact.name,
                                        )
                                        for f in pc.pending_update.get("new_facts", []) if f.get("fact")
                                    )
                                for parked_fact in parked_facts:
                                    text = await memory_engine.store_shared_fact(
                                        parked_fact, session_id=session_id, preresolved=preresolved
                                    )
                                    if text:
                                        saved_texts.append(text)
                                new_entity = ActiveEntity(
                                    id=new_contact.id, type="contact",
                                    name=new_contact.name, confidence=1.0,
                                    last_mentioned=time.time()
                                )
                                sess.active_entities = [new_entity]
                                sess.focus_entity = new_entity
                                disambiguation_resolved_note = (
                                    f"CONTACT CREATED: \"{pc.name}\" has been added to the user's contacts and the pending "
                                    f"information from their previous message has now been successfully saved. "
                                    f"CRITICAL: Confirm this naturally without sounding like it is an old memory (e.g. do NOT say 'we had previously noted'). Do NOT ask again."
                                    + _saved_just_now_clause(saved_texts)
                                )
                            except Exception as e:
                                logger.warning(f"Pending contact creation failed: {e}")
                            if sess.pending_creation is pc:  # re-routing may have parked a NEW question
                                sess.pending_creation = None
                            touch_session(session_id)
                        elif answer is False:
                            saved_texts = []
                            try:
                                for parked_fact in pc.pending_shared_facts:
                                    saved_texts.append(
                                        await memory_engine.apply_shared_fact_user_only(parked_fact, as_said_name=pc.name)
                                    )
                            except Exception as e:
                                logger.warning(f"User-only fact write after declined creation failed: {e}")
                            disambiguation_resolved_note = (
                                f"CONTACT CREATION DECLINED: The user chose not to add \"{pc.name}\" as a contact. "
                                f"Any fact involving the user from their previous message was successfully saved to their own memory instead. "
                                f"CRITICAL: Acknowledge naturally without sounding like it is an old memory. Do NOT ask again."
                                + _saved_just_now_clause(saved_texts)
                            )
                            sess.pending_creation = None
                            touch_session(session_id)
                        # answer is None → leave pending; the system prompt keeps the question alive
            # --- END DISAMBIGUATION / CREATION ---
            
            # Update ConversationSession deterministically.
            # Only replace active_entities when the current message resolves new entities;
            # otherwise retain existing pinned state.
            if bundle.resolved_entities and sess.pending_resolution is None:
                active_entities = [
                    ActiveEntity(
                        id=e.id, type=e.type, name=e.name,
                        confidence=e.confidence, last_mentioned=time.time()
                    )
                    for e in bundle.resolved_entities
                ]
                sess.active_entities = active_entities
                # focus_entity: single entity when unambiguous; None in comparison mode
                sess.focus_entity = active_entities[0] if len(active_entities) == 1 else None
                touch_session(session_id)
                
            memory_context = await memory_engine.format_context(bundle)
            pending_resolution = bundle.pending_resolution
            # Re-read from the session: the deterministic block above may have
            # just resolved/cleared what retrieve_context snapshotted earlier.
            sess_after = get_session(session_id)
            if sess_after.pending_resolution is None:
                pending_resolution = None
            else:
                # Always re-serialize: the block above may have re-parked with
                # fewer mentions, or converted a creation into a disambiguation.
                pr_now = sess_after.pending_resolution
                pending_resolution = {
                    "original_name": pr_now.original_name,
                    "candidates": pr_now.candidates,
                    "mentions": pr_now.mentions(),
                }
            if sess_after.pending_creation is not None and time.time() <= sess_after.pending_creation.expires:
                pending_creation = {"name": sess_after.pending_creation.name}
            ambiguous_mentions = bundle.ambiguous_mentions
        except Exception as e:
            logger.warning(f"Memory context build failed (non-critical): {e}")


    # Build message history with memory-enhanced system prompt
    messages: list[LLMMessage] = [
        LLMMessage(role="system", content=_build_system_prompt(
            memory_context, pending_resolution, disambiguation_resolved_note,
            pending_creation=pending_creation,
            ambiguous_mentions=ambiguous_mentions,
        ))
    ]
    for msg in request.messages:
        messages.append(LLMMessage(role=msg.role, content=msg.content))

    # Persist the user's message
    last_user_msg = user_msgs[-1] if user_msgs else None
    if last_user_msg:
        await _persist_message(db, session_id, "user", last_user_msg)

    async def event_generator():
        """Yields SSE-formatted chunks from the provider stream."""
        full_response = []

        try:
            async for delta in provider.stream_chat(messages):
                full_response.append(delta)
                chunk = StreamChunk(
                    delta=delta,
                    done=False,
                    session_id=session_id,
                    model=provider.model_name,
                    provider=provider.provider_name,
                )
                yield f"data: {chunk.model_dump_json()}\n\n"

        except Exception as e:
            logger.error(f"Streaming error: {e}")
            error_chunk = StreamChunk(
                delta=f"\n\n[Error: {str(e)}]",
                done=True,
                session_id=session_id,
            )
            yield f"data: {error_chunk.model_dump_json()}\n\n"
            return

        # Send final done event
        done_chunk = StreamChunk(
            delta="",
            done=True,
            session_id=session_id,
            model=provider.model_name,
            provider=provider.provider_name,
        )
        yield f"data: {done_chunk.model_dump_json()}\n\n"

        # Persist the complete assistant response
        complete_response = "".join(full_response)
        if complete_response:
            await _persist_message(
                db,
                session_id,
                "assistant",
                complete_response,
                model=provider.model_name,
            )

            # --- Phase 2: Fire background extraction pipeline
            if last_user_msg and complete_response:
                # Pass the full conversation so extractor can resolve vague references
                conversation_history = [
                    {"role": m.role, "content": m.content}
                    for m in request.messages
                ]
                background_tasks.add_task(
                    _run_extraction,
                    user_message=last_user_msg,
                    assistant_message=complete_response,
                    conversation_history=conversation_history,
                    session_id=session_id,
                    provider=provider,
                    qdrant=qdrant,
                )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _run_extraction(
    user_message: str,
    assistant_message: str,
    session_id: str,
    provider: LLMProvider,
    qdrant,
    conversation_history: list[dict] | None = None,
) -> None:
    """Background task: creates its own DB session to run extraction pipeline."""
    from app.db.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        engine = MemoryEngine(db=db, qdrant=qdrant)
        await run_extraction_pipeline(
            user_message=user_message,
            assistant_message=assistant_message,
            conversation_history=conversation_history or [],
            session_id=session_id,
            db=db,
            engine=engine,
            provider=provider,
        )


@router.post("", response_model=ChatResponse, summary="Non-streaming chat completion")
async def chat(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
    qdrant=Depends(get_qdrant),
) -> ChatResponse:
    """Non-streaming chat completion with memory context."""
    session_id = request.session_id or str(uuid.uuid4())

    memory_engine = MemoryEngine(db=db, qdrant=qdrant)
    last_user_msg = next(
        (m for m in reversed(request.messages) if m.role == "user"), None
    )
    memory_context = ""
    pending_resolution = None
    if last_user_msg:
        try:
            from app.memory.conversation_state import get_session, touch_session, ActiveEntity
            import time
            bundle = await memory_engine.retrieve_context(last_user_msg.content, session_id=session_id)
            
            # Update ConversationSession deterministically.
            # Only replace active_entities when the current message resolves new entities;
            # otherwise retain existing pinned state.
            if bundle.resolved_entities:
                sess = get_session(session_id)
                active_entities = [
                    ActiveEntity(
                        id=e.id, type=e.type, name=e.name,
                        confidence=e.confidence, last_mentioned=time.time()
                    )
                    for e in bundle.resolved_entities
                ]
                sess.active_entities = active_entities
                # focus_entity: single entity when unambiguous; None in comparison mode
                sess.focus_entity = active_entities[0] if len(active_entities) == 1 else None
                touch_session(session_id)
                
            memory_context = await memory_engine.format_context(bundle)
            pending_resolution = bundle.pending_resolution
        except Exception as e:
            logger.warning(f"Memory context build failed (non-critical): {e}")

    messages: list[LLMMessage] = [
        LLMMessage(role="system", content=_build_system_prompt(memory_context, pending_resolution))
    ]
    for msg in request.messages:
        messages.append(LLMMessage(role=msg.role, content=msg.content))

    if last_user_msg:
        await _persist_message(db, session_id, "user", last_user_msg.content)

    try:
        response = await provider.chat(messages)
    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    await _persist_message(
        db, session_id, "assistant", response.content,
        model=response.model, tokens_used=response.tokens_used,
    )

    return ChatResponse(
        content=response.content,
        model=response.model,
        provider=response.provider,
        session_id=session_id,
        tokens_used=response.tokens_used,
    )


@router.get("/sessions/{session_id}/messages", summary="Get chat history for a session")
async def get_session_messages(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Retrieve all messages for a given session ID."""
    from sqlalchemy import select
    result = await db.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at.asc())
    )
    messages = result.scalars().all()
    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "model": m.model,
            "created_at": m.created_at.isoformat(),
        }
        for m in messages
    ]
