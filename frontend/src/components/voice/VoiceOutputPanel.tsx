/**
 * Furi — Voice-mode output panel
 *
 * The reading surface inside voice mode: what Furi just said, and any
 * confirmation card it is waiting on, in a bounded scrollable box under the
 * sphere. Before this, voice mode rendered a sphere and one line of caption, so
 * every list, every paragraph and every approval sent the user back to the chat.
 *
 * ⚠️ TEXT IS THE CURRENT TURN ONLY — everything after the last `role === 'user'`
 * message. That single rule buys the auto-dismiss the feature needs for free:
 * speaking again (or clicking a card option, which echoes a user message)
 * starts a new turn, so the previous answer leaves on its own. No timer, no
 * clean-up pass, nothing to forget to cancel.
 *
 * ⚠️ CARDS ARE NOT SCOPED TO THE TURN, AND THAT IS DELIBERATE. A background
 * task that re-pauses is PATCHED onto its original message (chatStore's
 * receiveTaskEvent finds it by task_id), which by then can sit behind a later
 * user message — so a turn-scoped card list would silently hide a plan that is
 * still waiting on an answer, in the one surface where the chat is covered up.
 * A card leaves when it stops being actionable, which is the honest condition.
 * In practice there is at most one at a time.
 *
 * ⚠️ THIS COMPONENT OWNS ITS OWN SUBSCRIPTION, AND THAT IS LOAD-BEARING. It
 * reads message CONTENT, so it re-renders on every streamed delta.
 * VoiceModeOverlay deliberately subscribes to primitives only, so the canvas
 * sphere is not re-rendered dozens of times a second while a reply streams —
 * keep the delta-sensitive subscription down here.
 */
import { useCallback, useEffect, useMemo, useRef } from 'react';
import { Eye, X } from 'lucide-react';
import type { ChatMessage } from '@/types';
import { useChatStore } from '@/stores/chatStore';
import { isActionablePlanMessage } from '@/lib/planGate';
import { PlanCard } from '@/components/chat/PlanCard';
import { MarkdownBody } from '@/components/chat/MarkdownBody';

/** What the current turn has to show. A primitive so the overlay can select it
 *  without re-rendering the sphere on every delta. */
export type TurnOutputKind = 'none' | 'text' | 'card' | 'both';

/** Stands in for "the user has not said anything yet this session", so the
 *  hide-state key is still a stable string. */
const TURN_KEY_START = '__start__';

/** Index just past the last user message — the start of the turn being
 *  answered right now. */
function turnStart(messages: ChatMessage[]): number {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === 'user') return i + 1;
  }
  return 0;
}

/** Identity of the current turn. Changes the moment the user speaks or types
 *  again — which is what un-hides a reply the user had crossed out, without any
 *  code having to notice that a new turn began. */
export function currentTurnKey(messages: ChatMessage[]): string {
  const start = turnStart(messages);
  return start === 0 ? TURN_KEY_START : messages[start - 1].id;
}

/** MessageBubble's `hideTextBubble` rule (MessageBubble.tsx): a message whose
 *  plan paused at the gate IS its card — the streamed text repeats the same step
 *  list and goes stale once the plan resolves. Same rule here so the two
 *  surfaces cannot disagree about what a plan message looks like. */
function hidesOwnText(m: ChatMessage): boolean {
  return Boolean(m.plan && m.planNeededApproval);
}

function hasVisibleText(m: ChatMessage): boolean {
  return m.role === 'assistant' && !hidesOwnText(m) && m.content.trim() !== '';
}

export function turnOutputKind(messages: ChatMessage[]): TurnOutputKind {
  const card = messages.some(isActionablePlanMessage);
  let text = false;
  for (let i = turnStart(messages); i < messages.length; i++) {
    if (hasVisibleText(messages[i])) {
      text = true;
      break;
    }
  }
  if (card) return text ? 'both' : 'card';
  return text ? 'text' : 'none';
}

interface VoiceOutputPanelProps {
  /** The user crossed the text out for this turn. */
  textHidden: boolean;
  onToggleText: () => void;
}

export function VoiceOutputPanel({ textHidden, onToggleText }: VoiceOutputPanelProps) {
  const messages = useChatStore((s) => s.messages);
  const respondToPlan = useChatStore((s) => s.respondToPlan);
  const respondToChoice = useChatStore((s) => s.respondToChoice);
  const cancelBackgroundTask = useChatStore((s) => s.cancelBackgroundTask);
  const pauseBackgroundTask = useChatStore((s) => s.pauseBackgroundTask);

  const turnItems = useMemo(
    () => messages.slice(turnStart(messages)).filter(hasVisibleText),
    [messages]
  );
  // Session-wide, not turn-scoped — see the header.
  const cards = useMemo(() => messages.filter(isActionablePlanMessage), [messages]);
  const hasText = turnItems.length > 0;
  const hasCard = cards.length > 0;

  // Stick to the bottom while a reply streams — but never yank the box back
  // down under someone who has scrolled up to read the top of a long list.
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  const onScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [messages, textHidden]);

  // Text crossed out and nothing to answer: collapse to a chip rather than an
  // empty box, so the sphere gets its space back and the reply is still one
  // click away.
  if (textHidden && !hasCard) {
    if (!hasText) return null;
    return (
      <button
        type="button"
        onClick={onToggleText}
        className="shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-surface-2/80 border border-surface-border text-[11px] font-mono text-slate-400 hover:text-cyan-300 hover:border-cyan-500/40 transition-fast"
      >
        <Eye size={12} />
        Show reply
      </button>
    );
  }

  // ⚠️ THE PANEL YIELDS BEFORE THE SPHERE DOES. It is a shrinkable flex column
  // (`min-h-0`, no `shrink-0`) with a cap, so on a short window the parent
  // squeezes THIS and the body just scrolls further — MEASURED: with the panel
  // rigid instead, the sphere was down to 30px at 900x540, and the sphere is
  // the tap target.
  return (
    <div
      className="w-full max-w-2xl min-h-0 flex flex-col rounded-2xl border border-surface-border bg-surface-1/95 overflow-hidden animate-slide-up"
      style={{ maxHeight: 'min(38vh, 340px)', boxShadow: '0 -8px 32px rgba(4, 7, 16, 0.6)' }}
    >
      <div className="shrink-0 flex items-center justify-between px-3 py-1.5 border-b border-surface-border/70 bg-surface-2/60">
        <span className="text-[10px] font-mono uppercase tracking-[0.16em] text-cyan-400/70">
          Furi
        </span>
        {hasText && (
          <button
            type="button"
            onClick={onToggleText}
            title={
              textHidden
                ? 'Show the reply'
                : hasCard
                  ? 'Hide the text — the request below stays until you answer it'
                  : 'Hide this reply'
            }
            aria-label={textHidden ? 'Show the reply' : 'Hide this reply'}
            className="w-6 h-6 rounded-md flex items-center justify-center text-muted hover:text-cyan-300 hover:bg-surface-3 transition-fast"
          >
            {textHidden ? <Eye size={12} /> : <X size={13} />}
          </button>
        )}
      </div>

      {/* role="log", not aria-live: the caption above the sphere already
          announces state changes, and a second live region would make a screen
          reader read every streamed delta twice. */}
      <div
        ref={scrollRef}
        onScroll={onScroll}
        role="log"
        className="flex-1 min-h-0 overflow-y-auto px-4 py-3 space-y-3"
      >
        {!textHidden &&
          turnItems.map((m) => (
            <div key={m.id} className="selectable text-sm leading-relaxed text-slate-200">
              <MarkdownBody>{m.content}</MarkdownBody>
            </div>
          ))}

        {/* Identical wiring to MessageBubble — PlanCard reads no store, so the
            approval UI is the same component with the same callbacks, not a
            voice-mode reimplementation of consent. */}
        {cards.map((m) => (
          <PlanCard
            key={m.id}
            plan={m.plan!}
            responding={Boolean(m.planResponding)}
            error={m.planError ?? null}
            onRespond={(approved) => void respondToPlan(m.id, approved)}
            onAnswer={(answer) => void respondToChoice(m.id, answer)}
            cancelRequested={Boolean(m.planCancelRequested)}
            onCancelTask={() => void cancelBackgroundTask(m.id)}
            pauseRequested={Boolean(m.planPauseRequested)}
            onPauseTask={() => void pauseBackgroundTask(m.id)}
          />
        ))}
      </div>
    </div>
  );
}
