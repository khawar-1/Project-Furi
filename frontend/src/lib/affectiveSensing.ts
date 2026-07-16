/**
 * Jarvis OS — Affective Sensing collector (Phase 13.1)
 *
 * Client side of the coarse "user load" read. It summarizes TIMING and ENERGY
 * only — never keystroke content, never audio — and POSTs the summary to the
 * loopback backend, which derives a calm/steady/busy/stressed bucket in the
 * (in-memory, retention=none) World Model.
 *
 * Two locally-sensed sources feed the summary:
 *  - typing cadence: a window-level keydown listener records ONLY each keypress'
 *    timestamp + whether it was a delete (Backspace/Delete) + whether it was a
 *    content key. The character itself is never read or stored.
 *  - voice energy: voiceInput calls recordVoiceEnergy(rms) with its existing mic
 *    RMS meter while recording — an arousal proxy, not emotion. We keep the peak
 *    between posts.
 * (Activity intensity — app-switch rate — is derived server-side from the device
 * signals already flowing in; nothing to collect here.)
 *
 * Structurally gated: a post only happens when the context master AND
 * affective_sensing are both on (the backend ignores it otherwise, but we also
 * skip the request and drop buffered signal when off). No-op without a DOM.
 */
import { contextApi } from '@/lib/api';
import { useContextStore } from '@/stores/contextStore';

/** How often the rolling summary is computed and posted. */
const POST_INTERVAL_MS = 15_000;
/** The sliding window the cadence summary is computed over. */
const WINDOW_MS = 30_000;

interface Keypress {
  t: number;
  del: boolean;      // Backspace / Delete — the strain proxy
  content: boolean;  // a printable char or a delete (i.e. edits text)
}

let keypresses: Keypress[] = [];
let voicePeak = 0;
let timer: number | null = null;
let keyListener: ((e: KeyboardEvent) => void) | null = null;

/** Record the mic's scaled RMS (0..1) while recording — the voice-energy source.
 *  Cheap and always safe to call; only used while affective sensing is on. */
export function recordVoiceEnergy(rms: number): void {
  if (rms > voicePeak) voicePeak = rms;
}

function onKeyDown(e: KeyboardEvent): void {
  // Ignore pure modifier presses; record only timing + coarse category.
  if (e.key === 'Shift' || e.key === 'Control' || e.key === 'Alt' || e.key === 'Meta') {
    return;
  }
  const del = e.key === 'Backspace' || e.key === 'Delete';
  const content = del || e.key.length === 1; // printable char or an edit
  keypresses.push({ t: Date.now(), del, content });
}

function reset(): void {
  keypresses = [];
  voicePeak = 0;
}

function tick(): void {
  const settings = useContextStore.getState().settings;
  if (!settings?.enabled || !settings?.affective_sensing) {
    reset(); // off — never post, never retain buffered signal
    return;
  }

  const now = Date.now();
  keypresses = keypresses.filter((k) => now - k.t <= WINDOW_MS);

  const contentKeys = keypresses.filter((k) => k.content);
  const deletes = keypresses.filter((k) => k.del).length;
  const hasVoice = voicePeak > 0;

  // Nothing sensed this window — skip the request so an idle user posts nothing
  // (the World Model's user_state then stays dark = the graceful default).
  if (contentKeys.length === 0 && !hasVoice) {
    voicePeak = 0;
    return;
  }

  const signal: { typing_cpm?: number; backspace_rate?: number; voice_energy?: number } = {};
  if (contentKeys.length > 0) {
    signal.typing_cpm = contentKeys.length / (WINDOW_MS / 60_000);
    signal.backspace_rate = deletes / contentKeys.length;
  }
  if (hasVoice) signal.voice_energy = voicePeak;

  voicePeak = 0; // peak is per-interval
  void contextApi.postState(signal).catch(() => {
    // Best-effort — a failed post just means no fresh read this interval.
  });
}

/** Start the affective collector. Returns an unsubscribe fn. No-op without a
 *  DOM (plain-browser SSR / tests). */
export function initAffectiveSensing(): () => void {
  if (typeof window === 'undefined') return () => undefined;
  keyListener = onKeyDown;
  window.addEventListener('keydown', keyListener);
  timer = window.setInterval(tick, POST_INTERVAL_MS);
  return () => {
    if (keyListener) window.removeEventListener('keydown', keyListener);
    keyListener = null;
    if (timer !== null) window.clearInterval(timer);
    timer = null;
    reset();
  };
}
