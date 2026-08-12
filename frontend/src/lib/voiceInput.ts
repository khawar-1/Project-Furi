/**
 * Furi OS — Voice Input (Phase 7, Part 2)
 *
 * Push-to-talk microphone capture. One recording at a time: getUserMedia →
 * MediaRecorder (webm/opus — what faster-whisper's PyAV decoder expects) plus
 * an AnalyserNode feeding live level samples for the waveform.
 *
 * Semantics owned here, not by the UI:
 * - A hold shorter than MIN_RECORDING_MS is an accidental tap — stop()
 *   resolves null and the audio is discarded, nothing is transcribed.
 * - A hold longer than MAX_RECORDING_MS auto-stops (onAutoStop tells the UI
 *   to treat it as a release); the audio up to the cap is kept.
 * - cancel() discards everything (Esc), no callback fires afterwards.
 * - Part 5 (summon listening): with options.silenceStop, ~2s of quiet after
 *   speech (or 8s of never-speech) auto-stops like a release — hands-free.
 * - Part 6: with callbacks.onPartial, the accumulated buffer is offered
 *   every 1.5s for interim transcription (timeslice recording).
 *
 * The audio never leaves the machine: the caller posts the returned Blob to
 * the loopback-only backend.
 */
import { recordVoiceEnergy } from '@/lib/affectiveSensing';

export const MIN_RECORDING_MS = 300;
export const MAX_RECORDING_MS = 60_000;

// ---- Silence auto-stop (Phase 7, Part 5 — hands-free summon listening).
// Values tuned live: `level` is the meter's scaled RMS (×3.5, clamped 0..1);
// with echoCancellation+noiseSuppression, room noise sits well under 0.08
// while speech spikes past 0.15.
export const SILENCE_LEVEL_THRESHOLD = 0.08;
/** Once speech was heard, this long below the threshold ends the utterance. */
export const SILENCE_STOP_MS = 2_000;
/** A conversation's turn-taking gap (voice mode). Reported live: *"when i
 *  stopped talking its listning was still avtive for like 4 seconds"* — the 2s
 *  above is right for a one-shot dictated command and far too patient for
 *  back-and-forth, where the pause IS the handover. */
export const CONVERSATION_SILENCE_STOP_MS = 900;
/** Never heard speech at all — stop waiting after this long. */
export const MAX_INITIAL_SILENCE_MS = 8_000;

/**
 * Speech must be sustained this long before the window counts as an utterance.
 *
 * ⚠️ THIS IS WHAT MAKES AN ALWAYS-OPEN MIC SURVIVABLE. Voice mode holds the mic
 * open indefinitely, so a door, a cough or a keyboard clatter would otherwise
 * arm the detector, start the post-speech countdown, and send Whisper a second
 * of noise — which it answers with a hallucinated "Thank you." that becomes a
 * chat message nobody typed.
 *
 * The value sits in the gap between the two things it has to tell apart: an
 * impact transient is tens of milliseconds (a key click ~20ms, a knock ~50ms),
 * while the shortest real word is a single syllable at ~150-250ms. Deliberately
 * at the BOTTOM of the word range rather than the middle — the cost of admitting
 * a long thump is one junk turn the user can see and ignore, and the cost of
 * rejecting a spoken "no" is an answer that silently never arrives.
 */
export const MIN_SPEECH_MS = 150;

/**
 * The END-of-speech threshold adapts to the room; the ARM threshold does not.
 *
 * A fan, an air conditioner or a nearby laptop can hold the meter above a FIXED
 * 0.08 forever, and then the post-speech quiet gap never elapses and the mic
 * never closes — a worse failure than the one being fixed, because it looks like
 * Furi has stopped responding. So the quiet test uses a floor derived from the
 * quietest moment actually observed in this recording.
 *
 * Asymmetric on purpose, and this is the safety direction: ARMING still uses the
 * fixed threshold, so detecting that someone started talking is exactly as
 * sensitive as it has always been. Only the decision that they STOPPED consults
 * the room, and it is bounded — a recording that has not yet contained a quiet
 * moment can raise the bar at most to NOISE_CEILING, so the worst case is
 * ending an utterance a little eagerly, never refusing to hear one.
 */
export const NOISE_MULTIPLIER = 2.5;
export const NOISE_CEILING = 0.22;

// ---- Live partial transcripts (Phase 7, Part 6).
/** MediaRecorder timeslice: chunks accumulate steadily so a mid-recording
 *  Blob(chunks) is always a decodable stream (the first chunk carries the
 *  container header). The final blob is the same bytes it always was. */
export const PARTIAL_TIMESLICE_MS = 500;
/** How often the accumulated buffer is offered for interim transcription. */
export const PARTIAL_INTERVAL_MS = 1_500;

export interface RecorderCallbacks {
  /** Live input level 0..1, ~60/s while recording — drives the waveform. */
  onLevel?: (level: number) => void;
  /** A stop decided HERE (max cap, or silence in silenceStop mode) — the UI
   *  should treat it exactly like a release. */
  onAutoStop?: () => void;
  /** Part 6: the audio accumulated so far, every PARTIAL_INTERVAL_MS — for
   *  interim transcription. Never fires after stop()/cancel(). */
  onPartial?: (blob: Blob) => void;
}

export interface RecorderOptions {
  /** Part 5 (summon listening): auto-stop after SILENCE_STOP_MS of quiet once
   *  speech was heard (or MAX_INITIAL_SILENCE_MS if it never was). */
  silenceStop?: boolean;
  /** Phase 12.1 (continuous conversation): override the never-heard-speech
   *  grace. A follow-up window uses a shorter grace so a quiet user closes the
   *  conversation quickly. Defaults to MAX_INITIAL_SILENCE_MS.
   *
   *  `Infinity` means NEVER give up waiting — the open-mic mode voice mode uses.
   *  Only the MAX_RECORDING_MS cap ends such a window, and the caller re-opens. */
  initialSilenceMs?: number;
  /** Override the post-speech quiet gap that ends an utterance. Defaults to
   *  SILENCE_STOP_MS. */
  silenceStopMs?: number;
}

export interface RecordingHandle {
  /** Stop and collect the utterance. Resolves null for a too-short hold. */
  stop: () => Promise<Blob | null>;
  /** Discard the recording entirely (Esc / barge-in). Safe to call twice. */
  cancel: () => void;
  /** The mime type actually being recorded (for the upload filename). */
  mimeType: string;
  /**
   * Did anyone actually speak into this window? True once MIN_SPEECH_MS of
   * above-threshold audio has accumulated.
   *
   * ⚠️ THE CALLER USES THIS TO NOT TRANSCRIBE SILENCE. An always-open mic
   * closes on its 60s cap with nothing in it many times an hour; sending that to
   * Whisper costs a GPU round trip and returns either "" or an invented
   * sentence. Asking here is free.
   */
  heardSpeech: () => boolean;
}

/** The first recorder mime type this browser supports, opus preferred. */
function pickMimeType(): string {
  const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'];
  for (const c of candidates) {
    if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(c)) return c;
  }
  return ''; // let MediaRecorder pick its default
}

export function voiceCaptureSupported(): boolean {
  return (
    typeof navigator !== 'undefined' &&
    !!navigator.mediaDevices?.getUserMedia &&
    typeof MediaRecorder !== 'undefined'
  );
}

/**
 * Start recording. Rejects when the mic is unavailable or permission is
 * denied (the Electron main process only grants media to our own renderer).
 */
export async function startRecording(
  callbacks: RecorderCallbacks = {},
  options: RecorderOptions = {}
): Promise<RecordingHandle> {
  if (!voiceCaptureSupported()) {
    throw new Error('Microphone capture is not supported in this environment.');
  }

  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true },
  });

  const mimeType = pickMimeType();
  const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  const chunks: BlobPart[] = [];
  recorder.ondataavailable = (e: BlobEvent) => {
    if (e.data && e.data.size > 0) chunks.push(e.data);
  };

  // The blob settles when the recorder actually stops (dataavailable has
  // flushed by then) — stop() awaits this, and the max-cap auto-stop
  // resolves it early so a later stop() still gets the audio.
  let settleBlob: (blob: Blob) => void;
  const blobReady = new Promise<Blob>((resolve) => {
    settleBlob = resolve;
  });
  recorder.onstop = () => {
    settleBlob(new Blob(chunks, { type: mimeType || 'audio/webm' }));
  };

  // ---- Live level meter (AnalyserNode → RMS of the time-domain signal)
  const audioContext = new AudioContext();
  const analyser = audioContext.createAnalyser();
  analyser.fftSize = 512;
  audioContext.createMediaStreamSource(stream).connect(analyser);
  const samples = new Uint8Array(analyser.fftSize);
  const startedAt = Date.now();
  let finished = false;
  let cancelled = false;

  // Silence auto-stop state (only consulted when options.silenceStop): the
  // detector "arms" once MIN_SPEECH_MS of above-threshold audio has accumulated,
  // then a sustained quiet gap ends the utterance. Fires the same onAutoStop
  // path as the cap.
  let loudMs = 0;
  let lastFrameAt = startedAt;
  let lastLoudAt = startedAt;
  let autoStopFired = false;
  /** The quietest level seen so far — this recording's noise floor (see
   *  NOISE_MULTIPLIER). Starts at 1 so the first frame sets it. */
  let quietestLevel = 1;
  const silenceStopMs = options.silenceStopMs ?? SILENCE_STOP_MS;
  const initialSilenceMs = options.initialSilenceMs ?? MAX_INITIAL_SILENCE_MS;
  const spokeEnough = () => loudMs >= MIN_SPEECH_MS;

  let rafId = 0;
  const meter = () => {
    analyser.getByteTimeDomainData(samples);
    let sumSquares = 0;
    for (let i = 0; i < samples.length; i++) {
      const centered = (samples[i] - 128) / 128;
      sumSquares += centered * centered;
    }
    // RMS of speech is small — scale up and clamp so the waveform is lively.
    const level = Math.min(1, Math.sqrt(sumSquares / samples.length) * 3.5);
    callbacks.onLevel?.(level);
    // Phase 13: feed the arousal proxy (gated/consumed inside affectiveSensing).
    recordVoiceEnergy(level);
    const now = Date.now();
    const frameMs = Math.min(now - lastFrameAt, 100); // a throttled tab can gap
    lastFrameAt = now;
    if (level < quietestLevel) quietestLevel = level;
    // Arming stays on the FIXED threshold; only the quiet test adapts.
    if (level >= SILENCE_LEVEL_THRESHOLD) {
      loudMs += frameMs;
      lastLoudAt = now;
    }
    if (options.silenceStop && !autoStopFired && !finished && !cancelled) {
      const quietBar = Math.min(
        NOISE_CEILING,
        Math.max(SILENCE_LEVEL_THRESHOLD, quietestLevel * NOISE_MULTIPLIER)
      );
      if (level >= quietBar) {
        lastLoudAt = now;
      } else if (
        spokeEnough()
          ? now - lastLoudAt >= silenceStopMs
          : now - startedAt >= initialSilenceMs
      ) {
        autoStopFired = true;
        stopRecorder();
        callbacks.onAutoStop?.();
      }
    }
    rafId = requestAnimationFrame(meter);
  };
  rafId = requestAnimationFrame(meter);

  const teardown = () => {
    cancelAnimationFrame(rafId);
    if (partialTimer !== null) window.clearInterval(partialTimer);
    stream.getTracks().forEach((t) => t.stop());
    void audioContext.close().catch(() => undefined);
  };

  const stopRecorder = () => {
    if (recorder.state !== 'inactive') recorder.stop();
  };

  // Hard cap: never record forever because a pointerup was missed.
  const maxTimer = window.setTimeout(() => {
    if (finished || cancelled) return;
    stopRecorder();
    callbacks.onAutoStop?.();
  }, MAX_RECORDING_MS);

  // Part 6: offer the accumulated buffer for interim transcription. Chunks
  // from ONE recorder concatenate into a decodable stream (chunk 0 carries
  // the container header), so Blob(chunks) mid-recording is always valid.
  const partialTimer: number | null = callbacks.onPartial
    ? window.setInterval(() => {
        if (finished || cancelled || chunks.length === 0) return;
        callbacks.onPartial?.(new Blob(chunks, { type: mimeType || 'audio/webm' }));
      }, PARTIAL_INTERVAL_MS)
    : null;

  // The timeslice keeps chunks flowing for the partial timer; without it,
  // dataavailable fires only at stop. The assembled final blob is unchanged.
  recorder.start(PARTIAL_TIMESLICE_MS);

  return {
    mimeType: mimeType || 'audio/webm',

    heardSpeech: spokeEnough,

    stop: async (): Promise<Blob | null> => {
      if (cancelled) return null;
      finished = true;
      window.clearTimeout(maxTimer);
      const heldMs = Date.now() - startedAt;
      stopRecorder();
      const blob = await blobReady;
      teardown();
      // An accidental tap records nothing usable — discard, never transcribe.
      if (heldMs < MIN_RECORDING_MS || blob.size === 0) return null;
      return blob;
    },

    cancel: () => {
      if (cancelled || finished) return;
      cancelled = true;
      window.clearTimeout(maxTimer);
      stopRecorder();
      teardown();
    },
  };
}
