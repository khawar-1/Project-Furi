/**
 * Jarvis OS — Voice Input (Phase 7, Part 2)
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

export const MIN_RECORDING_MS = 300;
export const MAX_RECORDING_MS = 60_000;

// ---- Silence auto-stop (Phase 7, Part 5 — hands-free summon listening).
// Values tuned live: `level` is the meter's scaled RMS (×3.5, clamped 0..1);
// with echoCancellation+noiseSuppression, room noise sits well under 0.08
// while speech spikes past 0.15.
export const SILENCE_LEVEL_THRESHOLD = 0.08;
/** Once speech was heard, this long below the threshold ends the utterance. */
export const SILENCE_STOP_MS = 2_000;
/** Never heard speech at all — stop waiting after this long. */
export const MAX_INITIAL_SILENCE_MS = 8_000;

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
}

export interface RecordingHandle {
  /** Stop and collect the utterance. Resolves null for a too-short hold. */
  stop: () => Promise<Blob | null>;
  /** Discard the recording entirely (Esc / barge-in). Safe to call twice. */
  cancel: () => void;
  /** The mime type actually being recorded (for the upload filename). */
  mimeType: string;
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
  // detector "arms" on the first level above the threshold, then a sustained
  // quiet gap ends the utterance. Fires the same onAutoStop path as the cap.
  let heardSpeech = false;
  let lastLoudAt = startedAt;
  let autoStopFired = false;

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
    if (options.silenceStop && !autoStopFired && !finished && !cancelled) {
      const now = Date.now();
      if (level >= SILENCE_LEVEL_THRESHOLD) {
        heardSpeech = true;
        lastLoudAt = now;
      } else if (
        heardSpeech
          ? now - lastLoudAt >= SILENCE_STOP_MS
          : now - startedAt >= MAX_INITIAL_SILENCE_MS
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
