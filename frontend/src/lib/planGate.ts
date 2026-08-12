/**
 * Jarvis OS — "Is a plan waiting on the user?" — the two predicates, together
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
 */
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
