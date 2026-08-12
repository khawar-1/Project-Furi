/**
 * Furi OS — "Is a plan waiting on the user?" — the two predicates, together
 *
 * Both read only the chat store, and they are in their own module so that
 * voiceStore, voiceConversation, voiceModeControl and the overlay can all ask
 * without any of them importing each other. (voiceConversation imports
 * voiceStore, so voiceStore could not import it back.)
 *
 * ⚠️ TWO QUESTIONS, TWO PREDICATES, AND THE DIFFERENCE IS LOAD-BEARING.
 *
 *   mayOpenMic()      — "may the microphone open?"     counts `executing`
 *   awaitsDecision()  — "does the user owe an answer?" does NOT
 *
 * An executing plan owes the user nothing, so it must not put an approval banner
 * on screen; but it does own the turn, so the mic must not open over it. Kept in
 * one file precisely so the next person sees that they differ on purpose rather
 * than assuming one is a stale copy of the other.
 *
 * ⚠️ A THIRD SET LIVES HERE TOO, AND IT IS WIDER THAN BOTH.
 * `ACTIONABLE_PLAN_STATUSES` is about DISPLAY — which cards the voice-mode
 * panel puts in front of the user — and it includes `paused`, which neither
 * predicate above does. That is deliberate and NOT an oversight to "fix": a
 * paused plan does owe the user a carry-on or a correction (so its card must be
 * shown), but the mic must stay OPEN over it, because speaking a correction at a
 * paused plan already works — the backend routes the next message into the
 * parked plan. Adding `paused` to hasOpenInteractivePlan() would take that away.
 */
import type { ChatMessage, PlanStatus } from '@/types';
import { useChatStore } from '@/stores/chatStore';

/** A plan card the user must resolve — or one already running — is on screen.
 *  While one is, the card owns the interaction and the mic stays shut. */
export function hasOpenInteractivePlan(): boolean {
  return useChatStore.getState().messages.some(
    (m) =>
      m.planNeededApproval === true &&
      m.plan != null &&
      (m.plan.status === 'awaiting_approval' ||
        m.plan.status === 'awaiting_choice' ||
        m.plan.status === 'executing')
  );
}

/** Statuses whose card the user can still act on. DISPLAY only — see the
 *  header for why this one counts `paused` and the two predicates do not. */
export const ACTIONABLE_PLAN_STATUSES: readonly PlanStatus[] = [
  'awaiting_approval',
  'awaiting_choice',
  'paused',
];

/** True when this message carries a card the user can act on right now.
 *
 *  PURE and message-taking, unlike the two predicates, so it can be called
 *  inside a reactive Zustand selector — those read `getState()` and are
 *  snapshots by design. */
export function isActionablePlanMessage(m: ChatMessage): boolean {
  return (
    m.planNeededApproval === true &&
    m.plan != null &&
    ACTIONABLE_PLAN_STATUSES.includes(m.plan.status)
  );
}

/** The user owes a decision right now (approve, or answer a question). Excludes
 *  `executing`: nothing is being asked of them while a plan simply runs. */
export function awaitsDecision(): boolean {
  return useChatStore.getState().messages.some(
    (m) =>
      m.planNeededApproval === true &&
      m.plan != null &&
      (m.plan.status === 'awaiting_approval' || m.plan.status === 'awaiting_choice')
  );
}
