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
from app.memory.engine import MemoryEngine
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


def _build_system_prompt(
    memory_context: str = "",
    pending_resolution: dict = None,
    disambiguation_resolved_note: str = None,
    pending_creation: dict = None,
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
        names = [c["name"] for c in pending_resolution["candidates"]]
        base += f"""
PENDING DISAMBIGUATION:
A background process flagged "{pending_resolution['original_name']}" as ambiguous (possible matches: {', '.join(names)}).
However, check the latest user message first:
- If the user has ALREADY clearly stated which person they mean in their most recent message, accept it and proceed — do NOT ask again.
- If it is still unclear, ask the user to choose from: {', '.join(names)}. Do not invent alternatives.
"""
    elif pending_creation:
        base += f"""
PENDING CONTACT CREATION:
The user mentioned "{pending_creation['name']}", who is NOT in their contacts. Information about this person is on hold.
Ask the user exactly one question: "{pending_creation['name']} isn't in your contacts — want me to add them?"
Do not save or assume anything about this person until the user answers. If the user's latest message already answers this (yes/no), acknowledge and move on — do not ask again.
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
    disambiguation_resolved_note = None  # Injected into system prompt when resolved

    if recent_user_text:
        try:
            bundle = await memory_engine.retrieve_context(recent_user_text, session_id=session_id)
            
            # --- DETERMINISTIC DISAMBIGUATION RESOLUTION ---
            # If there's an active pending_resolution in the session, try to resolve the
            # user's latest message against the candidates IN PYTHON — before calling the LLM.
            # This prevents the LLM from asking the same question twice.
            sess = get_session(session_id)
            last_user_msg_content = user_msgs[-1] if user_msgs else ""
            
            if sess.pending_resolution is not None and last_user_msg_content:
                from app.memory.engine import resolve_confirmation
                pr = sess.pending_resolution
                all_contacts = await memory_engine.get_all_contacts()
                
                resolved_id = resolve_confirmation(last_user_msg_content, pr.candidates, all_contacts)
                
                if resolved_id == "NOT_FOUND_GLOBAL":
                    sess.pending_resolution = None
                    touch_session(session_id)
                    disambiguation_resolved_note = (
                        f"DISAMBIGUATION FAILED: The user replied '{last_user_msg_content}' which did not match "
                        f"any known contacts. Tell the user exactly this: 'This contact isn't in your contact list. "
                        f"If you want me to remember facts about them, please save them as a contact first.'"
                    )
                elif resolved_id is not None:
                    # Find the matched candidate name
                    resolved_name = next(
                        (c["name"] for c in pr.candidates if c["id"] == resolved_id),
                        last_user_msg_content
                    )
                    # Pin the resolved entity as the focus contact
                    all_contacts = await memory_engine.get_all_contacts()
                    resolved_contact = next((c for c in all_contacts if c.id == resolved_id), None)
                    if resolved_contact:
                        new_entity = ActiveEntity(
                            id=resolved_contact.id, type="contact",
                            name=resolved_contact.name, confidence=1.0,
                            last_mentioned=time.time()
                        )
                        sess.active_entities = [new_entity]
                        sess.focus_entity = new_entity

                    # Apply the parked writes deterministically — the whole point
                    # of deferring was to write them once the identity is known.
                    saved_something = False
                    try:
                        if resolved_contact:
                            if pr.pending_update:
                                await memory_engine.update_contact(resolved_contact.id, pr.pending_update)
                                saved_something = True
                            for parked_fact in pr.pending_shared_facts:
                                await memory_engine.apply_shared_fact_to_contact(parked_fact, resolved_contact)
                                saved_something = True
                    except Exception as e:
                        logger.warning(f"Applying parked writes after disambiguation failed: {e}")

                    # Clear pending resolution — it's been answered
                    sess.pending_resolution = None
                    touch_session(session_id)
                    # Tell the LLM exactly what happened
                    saved_note = (
                        f"The pending information WAS SAVED to \"{resolved_name}\" (and to the user's own memory where it involves them). "
                        if saved_something else
                        f"The original pending fact should now be applied to \"{resolved_name}\". "
                    )
                    disambiguation_resolved_note = (
                        f"DISAMBIGUATION RESOLVED: The user's reply \"{last_user_msg_content}\" "
                        f"has been matched to the contact \"{resolved_name}\". "
                        + saved_note +
                        f"Do NOT ask for clarification again. Acknowledge naturally and move on."
                    )

            # --- PENDING CONTACT CREATION: resolve the yes/no in Python ---
            elif sess.pending_creation is not None and last_user_msg_content:
                pc = sess.pending_creation
                if time.time() > pc.expires:
                    sess.pending_creation = None
                else:
                    answer = _interpret_yes_no(last_user_msg_content)
                    if answer is True:
                        try:
                            new_contact = await memory_engine.create_contact_manual(
                                pc.name,
                                {k: v for k, v in pc.pending_update.items() if k != "new_facts"},
                            )
                            if pc.pending_update.get("new_facts"):
                                await memory_engine.update_contact(
                                    new_contact.id, {"new_facts": pc.pending_update["new_facts"]}
                                )
                            for parked_fact in pc.pending_shared_facts:
                                await memory_engine.apply_shared_fact_to_contact(parked_fact, new_contact)
                            new_entity = ActiveEntity(
                                id=new_contact.id, type="contact",
                                name=new_contact.name, confidence=1.0,
                                last_mentioned=time.time()
                            )
                            sess.active_entities = [new_entity]
                            sess.focus_entity = new_entity
                            disambiguation_resolved_note = (
                                f"CONTACT CREATED: \"{pc.name}\" WAS ADDED to the user's contacts and the pending "
                                f"information WAS SAVED (to their fact log, and to the user's own memory where it "
                                f"involves the user). Confirm this naturally. Do NOT ask again."
                            )
                        except Exception as e:
                            logger.warning(f"Pending contact creation failed: {e}")
                        sess.pending_creation = None
                        touch_session(session_id)
                    elif answer is False:
                        try:
                            for parked_fact in pc.pending_shared_facts:
                                await memory_engine.apply_shared_fact_user_only(parked_fact, as_said_name=pc.name)
                        except Exception as e:
                            logger.warning(f"User-only fact write after declined creation failed: {e}")
                        disambiguation_resolved_note = (
                            f"CONTACT CREATION DECLINED: The user chose not to add \"{pc.name}\" as a contact. "
                            f"Any fact involving the user WAS still SAVED to the user's own memory. "
                            f"Acknowledge naturally and move on. Do NOT ask again."
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
            if sess_after.pending_creation is not None and time.time() <= sess_after.pending_creation.expires:
                pending_creation = {"name": sess_after.pending_creation.name}
        except Exception as e:
            logger.warning(f"Memory context build failed (non-critical): {e}")


    # Build message history with memory-enhanced system prompt
    messages: list[LLMMessage] = [
        LLMMessage(role="system", content=_build_system_prompt(
            memory_context, pending_resolution, disambiguation_resolved_note,
            pending_creation=pending_creation,
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
