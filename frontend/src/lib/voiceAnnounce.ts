/**
 * Jarvis OS — Proactive Speech (Phase 7, Part 5)
 *
 * The voice sibling of notifications.ts: server-pushed events (reminders,
 * briefings, task outcomes, birthdays) are SPOKEN when `speak_proactive` is
 * on. The text is the exact notificationContent() title+body the toast shows
 * — one derivation, two channels — and it rides the SAME Part 4 playback
 * queue, so an announcement serializes behind a response being spoken and
 * dies on every barge-in (mic press, new message, speaker off, stop button).
 *
 * Deliberate asymmetries with the toast channel:
 * - Toasts are suppressed while the window is focused (the visible app IS the
 *   notification); speech is NOT — being told out loud is the whole point of
 *   speak_proactive, whether or not you happen to be looking at Jarvis.
 * - Speech is additionally dropped while the mic is open (recording or
 *   transcribing): Jarvis never talks into its own microphone or over the
 *   user mid-utterance. Dropped, not deferred — the same best-effort contract
 *   as the push channel itself (the persisted chat message is the durable copy).
 */
import { onPush } from '@/lib/push';
import { notificationContent } from '@/lib/notifications';
import { speakText } from '@/lib/voiceOutput';
import { useVoiceStore } from '@/stores/voiceStore';

/** Start speaking push events aloud. Returns an unsubscribe function. */
export function initVoiceAnnounce(): () => void {
  return onPush('*', (event) => {
    const { settings, phase } = useVoiceStore.getState();
    if (!settings?.enabled || !settings.output_enabled || !settings.speak_proactive) return;
    // Never speak into an open mic — a recording (or a transcript about to
    // auto-send, whose reply will speak) owns the audio channel.
    if (phase !== 'idle') return;
    const content = notificationContent(event); // null = SILENT_TYPES / no type
    if (!content) return;
    // 'Jarvis' is notificationContent's FALLBACK title, not content — saying
    // it before every announcement is noise. A real title (reminders,
    // birthdays) is spoken as the lead-in sentence.
    speakText(content.title === 'Jarvis' ? content.body : `${content.title}. ${content.body}`);
  });
}
