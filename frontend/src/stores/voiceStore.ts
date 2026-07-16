/**
 * Jarvis OS — Voice State (Phase 7, Part 2)
 *
 * Push-to-talk state machine: idle → recording (mic held) → transcribing
 * (released, waiting on the backend) → idle. Voice is a TRANSPORT, never an
 * authority — a finished transcript enters the exact same chat pipeline as a
 * typed message (sendMessage), or lands in the input box when
 * review_before_send is on. Any voice failure degrades to an inline error;
 * it can never break a chat turn.
 *
 * Part 5 — summon listening: the global hotkey starts a hands-free recording
 * (mode 'summon') that auto-stops on silence; stopping routes through the
 * SAME endHold pipeline, so review_before_send and the speak-the-reply rule
 * apply unchanged. Part 6 — interim transcripts: while recording, the
 * accumulated audio is re-transcribed every ~1.5s (one request in flight at
 * a time) into `interimText`, which the final transcript replaces.
 */
import { create } from 'zustand';
import type { VoiceSettings } from '@/types';
import { voiceApi } from '@/lib/api';
import { startRecording, type RecordingHandle } from '@/lib/voiceInput';
import { markNextTurnVoice, stopSpeaking } from '@/lib/voiceOutput';
import { useChatStore } from '@/stores/chatStore';

export type VoicePhase = 'idle' | 'recording' | 'transcribing';
/** How the current recording started: a held mic ('hold'), the global hotkey
 *  or wake word ('summon' — tap-to-send, silence auto-stop), or an automatic
 *  follow-up window after a spoken reply ('followup' — Phase 12.1, silence
 *  auto-stop with a short grace; an empty follow-up ends the conversation
 *  silently rather than erroring). */
export type VoiceMode = 'hold' | 'summon' | 'followup';

/** Phase 12.1: the never-heard-speech grace for a follow-up window. Kept short
 *  so a quiet user closes the conversation quickly instead of waiting the full
 *  8s summon grace. */
const FOLLOWUP_INITIAL_SILENCE_MS = 5_000;

interface VoiceState {
  phase: VoicePhase;
  mode: VoiceMode;
  /** Live input level 0..1 while recording — drives the waveform. */
  level: number;
  /** Part 6: the interim transcript while still recording — display only,
   *  NEVER the draft (it must vanish without trace on cancel). */
  interimText: string;
  settings: VoiceSettings | null;
  /** Part 4: spoken audio is playing right now (mirrored by voiceOutput —
   *  drives the header "Speaking" indicator + stop button). */
  speaking: boolean;
  /** Phase 12.1: a voice conversation is in progress — the last voice turn was
   *  auto-sent and (while `continuous_conversation` is on) a follow-up window
   *  re-opens after each spoken reply. Cleared by Esc, a silent follow-up
   *  timeout, or disabling voice. The re-arm itself lives in
   *  lib/voiceConversation.ts (the voiceAnnounce module precedent). */
  conversationActive: boolean;
  /** Non-blocking inline error shown near the input; cleared on next hold. */
  error: string | null;

  /** Fetch settings once at startup (App.tsx); the settings card refreshes. */
  fetchSettings: () => Promise<void>;
  /** The settings card pushes its PUT response here so the mic reacts live. */
  applySettings: (settings: VoiceSettings) => void;
  beginHold: () => Promise<void>;
  /** Part 5: the hotkey summoned the window — start (or, if already summon-
   *  listening, stop-and-send) a hands-free recording. Opt-in via
   *  listen_on_summon; silence auto-stops it. */
  beginSummonListen: () => Promise<void>;
  /** Phase 12.2: the wake word ("Hey Jarvis") fired — start a hands-free
   *  recording. Opt-in via wake_word (NOT listen_on_summon); reuses the summon
   *  capture path so review/speak rules and continuous conversation all apply. */
  beginWakeListen: () => Promise<void>;
  /** Phase 12.1: after a spoken reply, re-open a short hands-free window so the
   *  user can talk back without re-triggering. Driven by voiceConversation.ts. */
  beginFollowUpListen: () => Promise<void>;
  /** Release: stop, transcribe, then auto-send (or draft, per settings). */
  endHold: () => Promise<void>;
  /** Esc / pointer left: discard the recording, transcribe nothing. */
  cancelHold: () => void;
  clearError: () => void;
}

/** The active recording — a live handle is process state, not render state. */
let activeRecording: RecordingHandle | null = null;
/** Poll timer while a model/voice downloads (enabled but not ready). */
let statusPollTimer: number | null = null;
/** Part 6: at most ONE interim transcription in flight — a busy tick is
 *  skipped, so slow CPUs self-pace instead of piling up requests. */
let partialBusy = false;
let partialAbort: AbortController | null = null;

export const useVoiceStore = create<VoiceState>((set, get) => {
  /** While voice is enabled but a model or voice is still loading, poll the
   *  (purely local, cheap) status endpoint so the mic tooltip and the
   *  settings card flip to ready on their own — the downloads were kicked
   *  server-side by the enabling PUT. One poller serves STT and TTS. */
  const maybePollVoiceStatus = () => {
    const { settings } = get();
    const needsPoll =
      settings?.enabled &&
      (settings.stt_status.status === 'loading' ||
        settings.tts_status.status === 'loading');
    if (!needsPoll || statusPollTimer !== null) return;
    statusPollTimer = window.setInterval(async () => {
      try {
        const status = await voiceApi.status();
        const current = get().settings;
        if (current) {
          set({
            settings: {
              ...current,
              stt_status: {
                status: status.stt.status,
                model: status.stt.model,
                error: status.stt.error,
              },
              tts_status: {
                status: status.tts.status,
                voice: status.tts.voice,
                error: status.tts.error,
                progress: status.tts.progress,
              },
            },
          });
        }
        if (status.stt.status !== 'loading' && status.tts.status !== 'loading') {
          if (statusPollTimer !== null) window.clearInterval(statusPollTimer);
          statusPollTimer = null;
        }
      } catch {
        // A transient error (backend briefly unreachable during boot, or a
        // uvicorn --reload) must NOT kill the poller: if it does, the card
        // freezes at the last 'loading' state and never flips to ready once
        // the model finishes loading. Skip this tick — the interval retries
        // in 3s and stops itself once both models report non-loading.
      }
    }, 3000);
  };

  /** Part 6: transcribe the accumulated buffer into interimText. Stale-safe:
   *  a result landing after the recording ended is dropped (phase check),
   *  and endHold aborts the in-flight request before the final transcribe. */
  const handlePartial = (blob: Blob) => {
    if (partialBusy || get().phase !== 'recording') return;
    partialBusy = true;
    partialAbort = new AbortController();
    voiceApi
      .transcribe(blob, partialAbort.signal)
      .then((result) => {
        if (get().phase === 'recording') {
          set({ interimText: result.text.trim() });
        }
      })
      .catch(() => undefined) // interim is best-effort; the final one reports
      .finally(() => {
        partialBusy = false;
        partialAbort = null;
      });
  };

  const stopPartials = () => {
    partialAbort?.abort();
    partialAbort = null;
    partialBusy = false;
  };

  /** Start a recording in any mode — beginHold/beginSummonListen/
   *  beginFollowUpListen share everything but the trigger + silence semantics.
   *  'summon' and 'followup' both auto-stop on silence; 'followup' uses a
   *  shorter never-heard-speech grace so a quiet user ends the conversation. */
  const startCapture = async (mode: VoiceMode) => {
    const { settings } = get();
    // Barge-in (Part 4): opening the mic silences Jarvis instantly —
    // you can't listen while you're being talked over.
    stopSpeaking();
    set({ error: null });
    const handsFree = mode === 'summon' || mode === 'followup';
    try {
      activeRecording = await startRecording(
        {
          onLevel: (level) => set({ level }),
          // A stop decided by voiceInput (60s cap, or silence in a hands-free
          // mode) is treated exactly like a release.
          onAutoStop: () => void get().endHold(),
          // Interim transcription only makes sense once the model is ready —
          // otherwise every tick would just 409.
          ...(settings?.stt_status.status === 'ready'
            ? { onPartial: handlePartial }
            : {}),
        },
        {
          silenceStop: handsFree,
          ...(mode === 'followup'
            ? { initialSilenceMs: FOLLOWUP_INITIAL_SILENCE_MS }
            : {}),
        }
      );
      set({ phase: 'recording', mode, level: 0, interimText: '' });
    } catch (e) {
      activeRecording = null;
      set({
        phase: 'idle',
        mode: 'hold',
        error:
          e instanceof Error && e.name === 'NotAllowedError'
            ? 'Microphone access was denied.'
            : e instanceof Error
              ? e.message
              : 'Could not start the microphone.',
      });
    }
  };

  return {
    phase: 'idle',
    mode: 'hold',
    level: 0,
    interimText: '',
    settings: null,
    speaking: false,
    conversationActive: false,
    error: null,

    fetchSettings: async () => {
      try {
        const settings = await voiceApi.getSettings();
        set({ settings });
        maybePollVoiceStatus();
      } catch {
        // Backend not up yet — voice simply stays unavailable until retried.
      }
    },

    applySettings: (settings: VoiceSettings) => {
      set({ settings });
      maybePollVoiceStatus();
    },

    beginHold: async () => {
      const { phase, settings } = get();
      if (phase !== 'idle') return;
      if (!settings?.enabled) return; // the button is disabled anyway
      await startCapture('hold');
    },

    beginSummonListen: async () => {
      const { phase, mode, settings } = get();
      if (!settings?.enabled || !settings.listen_on_summon) return;
      if (phase === 'recording') {
        // Pressing the hotkey again while already summon-listening is the
        // natural toggle: stop and send what was said.
        if (mode === 'summon') void get().endHold();
        return;
      }
      if (phase !== 'idle') return;
      await startCapture('summon');
    },

    beginWakeListen: async () => {
      const { phase, settings } = get();
      // Gated on the wake-word master (NOT listen_on_summon). wakeWord.ts
      // already suppresses detection while the mic is busy or Jarvis speaks,
      // but re-check phase here so a late trigger can never double-open.
      if (!settings?.enabled || !settings.wake_word) return;
      if (phase !== 'idle') return;
      await startCapture('summon');
    },

    beginFollowUpListen: async () => {
      const { phase, settings } = get();
      if (!settings?.enabled || !settings.continuous_conversation) return;
      if (phase !== 'idle') return;
      await startCapture('followup');
    },

    endHold: async () => {
      const recording = activeRecording;
      if (!recording || get().phase !== 'recording') return;
      activeRecording = null;
      // The mode the recording STARTED in decides the empty-transcript and
      // conversation semantics below (the transcribing set clears it to 'hold').
      const entryMode = get().mode;
      // The final transcript is authoritative — abort any interim request
      // so it never races the real one, and clear the interim display.
      stopPartials();
      set({ phase: 'transcribing', mode: 'hold', level: 0, interimText: '' });
      try {
        const blob = await recording.stop();
        if (!blob) {
          // Accidental tap (<300ms) — silently discard. A follow-up that was
          // too short to be speech ends the conversation quietly.
          set({ phase: 'idle' });
          if (entryMode === 'followup') set({ conversationActive: false });
          return;
        }
        const result = await voiceApi.transcribe(blob);
        const text = result.text.trim();
        set({ phase: 'idle' });
        if (!text) {
          // A silent follow-up window is the NATURAL end of a conversation —
          // close it quietly, never with a "didn't catch that" error.
          if (entryMode === 'followup') {
            set({ conversationActive: false });
            return;
          }
          set({ error: 'I didn’t catch anything — try again.' });
          return;
        }
        const chat = useChatStore.getState();
        // Review mode — and a turn already streaming — both land the words
        // in the input box instead: never auto-send, never lose a transcript.
        if (get().settings?.review_before_send || chat.isStreaming) {
          chat.setDraftMessage(text);
        } else {
          // A voice-initiated turn speaks its reply (Part 4). Review-mode
          // drafts are sent by Enter later — those count as typed turns.
          markNextTurnVoice();
          // Phase 12.1: mark the conversation active so voiceConversation.ts
          // re-opens a follow-up window after the reply is spoken.
          if (get().settings?.continuous_conversation) {
            set({ conversationActive: true });
          }
          await chat.sendMessage(text);
        }
      } catch (e) {
        // Transcription failure is non-blocking: recording discarded, chat
        // untouched. A 409 here already re-kicked the model load server-side.
        set({
          phase: 'idle',
          error: e instanceof Error ? e.message : 'Transcription failed.',
        });
        if (entryMode === 'followup') set({ conversationActive: false });
        void get().fetchSettings(); // refresh the model status the error names
      }
    },

    cancelHold: () => {
      activeRecording?.cancel();
      activeRecording = null;
      stopPartials();
      // Esc ends any in-progress voice conversation (Phase 12.1).
      if (get().phase !== 'idle') {
        set({ phase: 'idle', mode: 'hold', level: 0, interimText: '', conversationActive: false });
      } else if (get().conversationActive) {
        set({ conversationActive: false });
      }
    },

    clearError: () => set({ error: null }),
  };
});
