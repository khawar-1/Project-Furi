/**
 * Jarvis OS — Voice Mode policy (the ONE definition)
 *
 * Voice mode is a full-screen, hands-free conversation surface: the sphere takes
 * over the chat's interaction area and the loop runs listen → send → speak →
 * listen. Making that loop run requires WIDENING four gates that are normally
 * governed by persisted settings.
 *
 * ⚠️ NOTHING HERE IS EVER PERSISTED. Voice mode carries its own intent for as
 * long as it is open — no PUT is issued, no setting is rewritten — so leaving it
 * restores exactly the behaviour the user configured. The override lives in the
 * predicate, not in the database.
 *
 * ⚠️ ONE DEFINITION, FOUR READERS. Four inline `|| isVoiceMode` checks scattered
 * across voiceStore / voiceConversation / voiceOutput is precisely the
 * "a second copy of a fact is a hole" defect this project has recorded seven
 * times (registry.mutates, _DIR_KEY, _settle's status tuple, set_voice_config…).
 * Every gate reads a function from this file.
 *
 * `settings.enabled` is deliberately NOT overridable: it is a real capability
 * gate (the STT/TTS models are only loaded server-side when it is on), not a
 * preference. With voice off, the entry point is disabled and points at Settings.
 */
import { useUIStore } from '@/stores/uiStore';
import type { VoiceSettings } from '@/types';

/**
 * Voice mode is open on screen right now.
 *
 * ⚠️ THE PANEL CHECK IS THE LEAK GUARANTEE. A stale `isVoiceMode === true`
 * would silently override the user's persisted settings app-wide, and putting
 * that guarantee in an unmount cleanup does NOT work: React StrictMode
 * double-invokes effects in dev, so a cleanup that closed voice mode would fire
 * the instant it opened. Deriving it instead means the override is inert the
 * moment the chat is not what's on screen, whatever happens to the flag.
 */
export function voiceModeActive(): boolean {
  const ui = useUIStore.getState();
  return ui.isVoiceMode && ui.activePanel === 'chat';
}

/**
 * The hands-free re-arm loop should run — i.e. after a spoken reply settles, a
 * fresh listening window opens on its own. Normally opt-in via
 * `continuous_conversation`; voice mode IS that intent, expressed by being open.
 */
export function voiceLoopActive(s: VoiceSettings | null): boolean {
  return !!s?.enabled && (s.continuous_conversation || voiceModeActive());
}

/**
 * Replies should be spoken aloud. Normally `output_enabled` (the header speaker
 * toggle); a voice conversation with no voice is not a conversation, so voice
 * mode implies it — without writing the toggle the user set.
 */
export function voiceOutputActive(s: VoiceSettings | null): boolean {
  return !!s?.enabled && (s.output_enabled || voiceModeActive());
}

/**
 * A finished transcript auto-sends instead of landing in the input box.
 * `review_before_send` exists so a typed-surface user can proofread; in voice
 * mode there is no input box in front of the user to proofread in.
 */
export function voiceAutoSend(s: VoiceSettings | null): boolean {
  return !s?.review_before_send || voiceModeActive();
}

/**
 * EVERY reply is spoken, not only the ones a voice turn asked for. Normally
 * opt-in via `speak_all_responses`; in voice mode a reply the user cannot hear
 * is a reply they cannot receive — the chat is behind the sphere. This is what
 * makes a message TYPED in the voice composer speak its answer.
 */
export function voiceSpeakEveryTurn(s: VoiceSettings | null): boolean {
  return !!s?.speak_all_responses || voiceModeActive();
}
