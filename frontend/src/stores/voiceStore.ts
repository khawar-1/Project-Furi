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
import {
  CONVERSATION_SILENCE_STOP_MS,
  startRecording,
  type RecordingHandle,
} from '@/lib/voiceInput';
import { markNextTurnVoice, speakText, stopSpeaking } from '@/lib/voiceOutput';
import { tryApproveByVoice } from '@/lib/spokenApproval';
import { hasOpenInteractivePlan } from '@/lib/planGate';
import { voiceAutoSend, voiceLoopActive, voiceModeActive } from '@/lib/voiceMode';
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
 *  8s summon grace. Does NOT apply while voice mode is open — see OPEN_MIC_GRACE. */
const FOLLOWUP_INITIAL_SILENCE_MS = 5_000;

/**
 * ⚠️ THE OPEN-MIC RULE — the fix for "it stops listening after the first time
 * I speak".
 *
 * A follow-up window used to give the user FOLLOWUP_INITIAL_SILENCE_MS to start
 * talking and then end the conversation for good; from the user's chair, one
 * exchange worked and everything after it needed a tap. Five seconds is a
 * perfectly ordinary pause after hearing an answer, so the loop was ending on
 * the most normal thing a person does.
 *
 * While voice mode is open the mic simply does not time out. Voice mode is an
 * explicit, sustained, visible intent — a full-screen sphere the user opened and
 * can close with one key — so "keep listening until I leave" is what being open
 * MEANS. Nothing here is persisted; leaving restores the configured behaviour
 * (lib/voiceMode.ts).
 *
 * The MAX_RECORDING_MS cap still bounds any single window; a window that closes
 * on the cap having heard nothing is re-opened by endHold without spending a
 * transcription on the silence.
 */
const OPEN_MIC_GRACE = Number.POSITIVE_INFINITY;

/**
 * Whisper's averaged `no_speech_prob` at or above which a hands-free transcript
 * is discarded as room noise rather than sent.
 *
 * Deliberately high. Below this the transcript is sent even if the model was
 * unsure, because the cost of dropping something the user really said (they
 * repeat themselves, and wonder whether Jarvis is broken) is worse than the cost
 * of an occasional junk turn (they see it and move on).
 */
const NO_SPEECH_DROP_PROB = 0.7;

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
   *  user can talk back without re-triggering. Driven by voiceConversation.ts.
   *  `initialSilenceMs` overrides the never-heard-speech grace — voice mode's
   *  FIRST window passes the longer summon grace, because a cold entry deserves
   *  more than the mid-conversation 5s before it gives up. */
  beginFollowUpListen: (initialSilenceMs?: number) => Promise<void>;
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
    // ⚠️ NEVER TRANSCRIBE AN IDLE MIC. Voice mode holds the window open until
    // someone speaks, and a partial fires every PARTIAL_INTERVAL_MS regardless —
    // so without this gate an open mic runs Whisper on silence continuously,
    // competing for the same GPU the real transcription needs and heating a
    // laptop that is already short of VRAM. Costs nothing to ask.
    if (!activeRecording?.heardSpeech()) return;
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
  const startCapture = async (mode: VoiceMode, initialSilenceMs?: number) => {
    const { settings } = get();
    // Barge-in (Part 4): opening the mic silences Jarvis instantly —
    // you can't listen while you're being talked over.
    stopSpeaking();
    set({ error: null });
    const handsFree = mode === 'summon' || mode === 'followup';
    // An explicit grace wins; then the OPEN-MIC rule (voice mode never times
    // out); then the short mid-conversation one; then voiceInput's default.
    const grace =
      initialSilenceMs ??
      (mode === 'followup'
        ? voiceModeActive()
          ? OPEN_MIC_GRACE
          : FOLLOWUP_INITIAL_SILENCE_MS
        : undefined);
    // A conversation's turn-taking pause is much shorter than a dictated
    // command's. Scoped to 'followup' deliberately: the report was about voice
    // mode, and cutting a summoned "remind me at 6 to call Jamil" off at a
    // mid-sentence breath would be a regression bought with someone else's bug.
    const quietGap = mode === 'followup' ? CONVERSATION_SILENCE_STOP_MS : undefined;
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
          ...(grace !== undefined ? { initialSilenceMs: grace } : {}),
          ...(quietGap !== undefined ? { silenceStopMs: quietGap } : {}),
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

  /**
   * A hands-free window produced nothing while voice mode is open → open another
   * one instead of ending the conversation. Returns true when it took over.
   *
   * ⚠️ IT STILL RESPECTS THE APPROVAL GATE. A plan can pause for approval DURING
   * a listening window (a background task hitting a write), and re-opening over
   * its card would put the mic on top of a decision the user has to see. Same
   * predicate voiceConversation's re-arm uses (lib/planGate.ts), so the two
   * cannot drift.
   *
   * Terminating: this is only ever reached from endHold, i.e. after a window has
   * actually closed, and a failed startCapture lands in 'idle' with an error and
   * queues nothing further — so there is no path that spins.
   *
   * Goes through beginFollowUpListen rather than startCapture directly, for its
   * `phase !== 'idle'` guard: endHold sets 'idle' and the capture is async, so a
   * concurrent open (an Esc, a tap) would otherwise overwrite `activeRecording`
   * and leak the first recording's stream.
   */
  const reopenOpenMic = (entryMode: VoiceMode): boolean => {
    if (entryMode !== 'followup' || !voiceModeActive()) return false;
    if (hasOpenInteractivePlan()) return false;
    void get().beginFollowUpListen();
    return true;
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

    beginFollowUpListen: async (initialSilenceMs?: number) => {
      const { phase, settings } = get();
      // voiceLoopActive: the persisted continuous_conversation setting OR an
      // open voice mode, which IS that intent (lib/voiceMode.ts).
      if (!voiceLoopActive(settings)) return;
      if (phase !== 'idle') return;
      await startCapture('followup', initialSilenceMs);
    },

    endHold: async () => {
      const recording = activeRecording;
      if (!recording || get().phase !== 'recording') return;
      activeRecording = null;
      // The mode the recording STARTED in decides the empty-transcript and
      // conversation semantics below (the transcribing set clears it to 'hold').
      const entryMode = get().mode;
      // ⚠️ EVERY "was that really speech?" GUARD BELOW IS HANDS-FREE ONLY.
      // A held mic is the user deliberately asking to be heard — discarding
      // that because a meter or a model was unsure would be a far worse bug
      // than the noise turns these guards exist to prevent. (And the meter can
      // genuinely be unsure: a softly-spoken hold sits near the threshold.)
      const handsFree = entryMode === 'summon' || entryMode === 'followup';
      // Asked BEFORE stop(), while the meter's state is still the window's own.
      const spoke = recording.heardSpeech();
      // The final transcript is authoritative — abort any interim request
      // so it never races the real one, and clear the interim display.
      stopPartials();
      set({ phase: 'transcribing', mode: 'hold', level: 0, interimText: '' });
      try {
        const blob = await recording.stop();
        if (!blob || (handsFree && !spoke)) {
          // Nothing was said: an accidental tap (<300ms), or a hands-free window
          // that closed on its cap or its grace having heard only the room.
          // ⚠️ NOT SENT TO WHISPER. Transcribing silence costs a GPU round trip
          // and returns either "" or an invented sentence — and in an open mic
          // that invented sentence would become a chat message nobody said.
          set({ phase: 'idle' });
          if (reopenOpenMic(entryMode)) return;
          if (entryMode === 'followup') set({ conversationActive: false });
          return;
        }
        const result = await voiceApi.transcribe(blob);
        // ⚠️ WHISPER'S OWN VERDICT, not a list of phrases we guessed it invents.
        // Given a window of room noise it does not return "" — it returns a
        // confident short sentence ("Thank you.", "Bye."), and in an always-open
        // mic that becomes a chat message nobody said. `no_speech_prob` is the
        // model reporting that it heard no speech, which is a real comparator.
        // Hands-free only, for the reason given where `handsFree` is computed.
        const noise =
          handsFree && (result.no_speech_prob ?? 0) >= NO_SPEECH_DROP_PROB;
        const text = noise ? '' : result.text.trim();
        set({ phase: 'idle' });
        if (!text) {
          // While voice mode is open, keep listening — an empty transcript is
          // just a noise that fooled the meter, not the end of a conversation.
          if (reopenOpenMic(entryMode)) return;
          // A silent follow-up window is the NATURAL end of a conversation —
          // close it quietly, never with a "didn't catch that" error.
          if (entryMode === 'followup') {
            set({ conversationActive: false });
            return;
          }
          set({ error: 'I didn’t catch anything — try again.' });
          return;
        }
        // ⚠️ SPOKEN APPROVAL, and ONLY for a voice-originated turn. If a
        // contract was read aloud moments ago, offer these words as consent to
        // it. The SERVER decides — whether they are consent at all, whether the
        // setting allows it, and whether the hash still matches the pending
        // steps — so a refusal is not a failure: the words fall through to the
        // normal chat path and become a steer, or the typed-approval nudge.
        // Nothing is ever swallowed.
        //
        // Deliberately NOT offered for review-mode drafts: those are sent by
        // Enter later, which makes them typed turns at a screen where the card
        // is right there.
        if (!get().settings?.review_before_send) {
          const spoken = await tryApproveByVoice(text);
          if (spoken.kind === 'approved') {
            // ⚠️ THE ANSWER MUST LAND SOMEWHERE. Until 2026-08-04 the returned
            // plan was DISCARDED here: the card kept offering Approve for a
            // plan the backend had already consumed (clicking it 404'd), the
            // outcome text was never rendered, and — in the one feature whose
            // whole point is not needing a screen — Jarvis said nothing at
            // all. The card path has done both since 2026-07-12
            // (chatStore.respondToPlan); this is the same two steps.
            const outcome = useChatStore.getState().applyApprovedPlan(spoken.plan);
            // Speak it: this was a voice turn, so the reply has to be audible.
            // Null for a BACKGROUND task, whose outcome arrives by push and is
            // spoken by voiceAnnounce — which is what stops a double-speak.
            if (outcome) speakText(outcome);
            set({ conversationActive: false });
            return;
          }
        }
        const chat = useChatStore.getState();
        // Review mode — and a turn already streaming — both land the words
        // in the input box instead: never auto-send, never lose a transcript.
        // voiceAutoSend: review_before_send exists so a typed-surface user can
        // proofread; in voice mode there is no input box in front of them.
        if (!voiceAutoSend(get().settings) || chat.isStreaming) {
          chat.setDraftMessage(text);
        } else {
          // A voice-initiated turn speaks its reply (Part 4). Review-mode
          // drafts are sent by Enter later — those count as typed turns.
          markNextTurnVoice();
          // Phase 12.1: mark the conversation active so voiceConversation.ts
          // re-opens a follow-up window after the reply is spoken.
          if (voiceLoopActive(get().settings)) {
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
