/**
 * Jarvis OS — Voice Mode entry / exit
 *
 * The two transitions, in one place, so every trigger (the composer button, the
 * X, Escape, the sidebar item, an unmount) does exactly the same thing.
 *
 * Kept separate from lib/voiceMode.ts on purpose: that module is PURE POLICY
 * (it answers "is the loop allowed to run?" and is read by voiceStore itself),
 * and importing voiceStore into it would close an import cycle. Nothing in the
 * app imports this file except UI.
 */
import { hasOpenInteractivePlan } from '@/lib/planGate';
import { cancelTurn, stopSpeaking } from '@/lib/voiceOutput';
import { useChatStore } from '@/stores/chatStore';
import { useUIStore } from '@/stores/uiStore';
import { useVoiceStore } from '@/stores/voiceStore';
import type { VoiceSettings } from '@/types';

/**
 * Voice mode can be opened at all. `enabled` is a real capability gate — the
 * STT/TTS models are only loaded server-side when it is on — so unlike every
 * other voice setting it is NOT overridable by opening voice mode.
 */
function canEnterVoiceMode(settings: VoiceSettings | null): boolean {
  return !!settings?.enabled;
}

/**
 * Open voice mode and, when the moment is right, start listening immediately.
 *
 * The mic is deliberately NOT opened when Jarvis is mid-reply (that would cut
 * him off the instant you asked to hear him) or when an approval card is
 * waiting (a decision the user must see). In both cases the sphere appears in
 * the matching state and the existing conversation loop takes over on its own.
 */
export function enterVoiceMode(): void {
  const ui = useUIStore.getState();
  if (ui.isVoiceMode) return;
  const voice = useVoiceStore.getState();
  if (!canEnterVoiceMode(voice.settings)) return;

  // Set the flag FIRST: every gate in lib/voiceMode.ts reads it, so the
  // listen call below must happen in a world where voice mode is already open.
  ui.setVoiceMode(true);

  if (
    voice.phase === 'idle' &&
    !voice.speaking &&
    !useChatStore.getState().isStreaming &&
    !hasOpenInteractivePlan()
  ) {
    // No grace argument: while voice mode is open the mic does not time out at
    // all (voiceStore's OPEN-MIC rule), so there is no "first window is more
    // patient" special case left to make.
    void voice.beginFollowUpListen();
  }
}

/**
 * Close voice mode: stop listening, stop speaking, restore the chat.
 *
 * Deliberately unguarded and idempotent — it is also the overlay's unmount
 * cleanup, and a stale `isVoiceMode === true` would silently override the
 * user's persisted settings app-wide. Clearing the flag BEFORE closing the mic
 * means the conversation loop cannot re-arm during teardown.
 *
 * Nothing is lost: every voice turn is already an ordinary chatStore message,
 * so the chat is simply revealed with the conversation continued.
 */
export function exitVoiceMode(): void {
  useUIStore.getState().setVoiceMode(false);
  useVoiceStore.getState().cancelHold();
  stopSpeaking();
}

/**
 * The one gesture of voice mode — tapping the sphere or the composer's mic.
 * Both surfaces mean the same thing, so both call this rather than keeping a
 * second copy of the rules.
 *
 * - listening  → send what has been said, without waiting for the pause
 * - speaking   → barge in: silence Jarvis and take the floor
 * - idle       → start talking
 * - thinking   → nothing; there is nothing useful to interrupt
 */
export function toggleVoiceListening(): void {
  const voice = useVoiceStore.getState();

  if (voice.phase === 'recording') {
    void voice.endHold();
    return;
  }
  if (voice.phase === 'transcribing') return;

  if (voice.speaking) {
    // ⚠️ cancelTurn BEFORE stopSpeaking. stopSpeaking only clears what is
    // already queued; the turn would keep enqueueing the sentences still
    // streaming in, and Jarvis would start talking again a second later.
    cancelTurn();
    stopSpeaking();
  }
  // A reply still being generated is left to finish — its remaining text is
  // real, and the conversation loop re-opens the mic the moment it lands.
  if (useChatStore.getState().isStreaming) return;

  void voice.beginFollowUpListen();
}

/**
 * Send a message TYPED into the voice-mode composer.
 *
 * Marking the conversation active is what keeps the hands-free loop going: the
 * reply is spoken (voiceSpeakEveryTurn) and the mic re-opens after it, so
 * reaching for the keyboard for one awkward word — a name, a URL — does not
 * drop you out of the conversation.
 */
export function sendFromVoiceMode(text: string): void {
  useVoiceStore.setState({ conversationActive: true });
  void useChatStore.getState().sendMessage(text);
}
