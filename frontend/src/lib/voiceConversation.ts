/**
 * Jarvis OS — Continuous Conversation (Phase 12.1)
 *
 * A store-subscriber controller (the same init-module pattern the output
 * router uses) that re-opens a short hands-free listening window AFTER a
 * voice-initiated reply has been
 * spoken, so the user can talk back without re-triggering (hotkey/hold/wake).
 * If the user speaks, it flows through the SAME endHold pipeline as another
 * voice turn (whose reply speaks → this re-arms → a natural back-and-forth);
 * if the follow-up window elapses in silence, endHold ends the conversation
 * quietly (no error).
 *
 * Keeping the re-arm here (not in voiceStore) mirrors voiceAnnounce: the store
 * stays a lean state machine; this module owns the cross-store timing.
 *
 * Safety properties:
 * - Only re-arms when the reply ACTUALLY SPOKE (spokeThisTurn). A turn that
 *   produced no speech — a PlanCard awaiting approval, output disabled — never
 *   opens the mic over a decision the user must make.
 * - Never re-arms while an interactive plan (approval / clarifying question /
 *   executing) is on screen: the card owns the interaction, not the mic.
 * - Debounced past inter-sentence gaps, so a momentary lull between two spoken
 *   sentences is never mistaken for the end of the reply.
 * - Gated on enabled + output_enabled + continuous_conversation, and OFF under
 *   review_before_send (a review turn goes to the draft, not an auto-send loop).
 */
import { useVoiceStore } from '@/stores/voiceStore';
import { useChatStore } from '@/stores/chatStore';
import { hasOpenInteractivePlan } from '@/lib/planGate';
import { voiceAutoSend, voiceLoopActive, voiceOutputActive } from '@/lib/voiceMode';

/** Wait this long after speech stops before opening the follow-up window — long
 *  enough to ride over the tiny gaps between two streamed sentences, short
 *  enough to feel immediate. */
const ARM_DEBOUNCE_MS = 700;

let spokeThisTurn = false;
let armTimer: number | null = null;

function clearTimer(): void {
  if (armTimer !== null) {
    window.clearTimeout(armTimer);
    armTimer = null;
  }
}

/** Re-evaluate the full predicate against LIVE state (not the transition that
 *  scheduled us) and open the follow-up window if everything still holds. */
function tryArm(): void {
  armTimer = null;
  const v = useVoiceStore.getState();
  const s = v.settings;
  if (
    v.conversationActive &&
    !v.speaking &&
    v.phase === 'idle' &&
    spokeThisTurn &&
    !useChatStore.getState().isStreaming &&
    // The three settings gates, each read through lib/voiceMode.ts so an open
    // voice mode widens them all in ONE place rather than three inline checks.
    voiceOutputActive(s) &&
    voiceLoopActive(s) &&
    voiceAutoSend(s) &&
    !hasOpenInteractivePlan()
  ) {
    spokeThisTurn = false;
    void v.beginFollowUpListen();
  }
}

function schedule(): void {
  if (armTimer !== null) return; // a check is already pending; tryArm re-reads live
  armTimer = window.setTimeout(tryArm, ARM_DEBOUNCE_MS);
}

/** Start the continuous-conversation controller. Returns an unsubscribe fn. */
export function initVoiceConversation(): () => void {
  const unsub = useVoiceStore.subscribe((state, prev) => {
    // A reply began speaking — this turn is eligible to re-arm once it settles.
    if (state.speaking && !prev.speaking) spokeThisTurn = true;
    // The conversation ended (Esc, silent follow-up, disabled) — reset.
    if (!state.conversationActive) {
      spokeThisTurn = false;
      clearTimer();
      return;
    }
    // Any move toward the armable state (speech stopping, phase → idle) schedules
    // a debounced re-check; tryArm makes the real decision against live state.
    schedule();
  });
  return () => {
    unsub();
    clearTimer();
    spokeThisTurn = false;
  };
}
