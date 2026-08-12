/**
 * Jarvis OS — Wake phrase detection by transcript ("Furi", "Hey Furi")
 *
 * ⚠️ WHY THIS EXISTS RATHER THAN A SECOND ONNX MODEL. openWakeWord's classifier
 * is trained per PHRASE: the bundled `hey_jarvis_v0.1.onnx` recognises "Hey
 * Jarvis" and nothing else, and no setting can change that — the phrase is in the
 * weights. The user calls the assistant Furi. Training a new classifier is an
 * offline job (synthetic voices, a Colab notebook, an hour), so the phrase would
 * have stayed unchangeable until someone did it.
 *
 * The alternative that works today: let the SPEECH RECOGNISER read the phrase.
 * An always-on energy gate (no model, no network, negligible cost) notices that
 * somebody spoke; only then is ~1-3s of audio transcribed locally and checked
 * against a phrase the user can type. That makes the wake word a SETTING instead
 * of a build artefact.
 *
 * THE COST, STATED: one local transcription per short burst of speech in the
 * room, rather than the ONNX model's near-zero. Bounded three ways — the gate
 * only fires on real energy, only 0.3-3.0s utterances are sent (a wake phrase is
 * short; ordinary conversation produces long bursts that are skipped outright),
 * and detection is suspended whenever the mic is otherwise in use. `wake_mode`
 * keeps the cheap model path available for anyone who trains a phrase.
 *
 * PRIVACY IS UNCHANGED: the audio goes to the SAME loopback-only local endpoint
 * every voice turn already uses. Nothing leaves the machine.
 *
 * Everything here is pure and testable. The mic, the worker and the lifecycle
 * stay in wakeWord.ts.
 */

// ---------------------------------------------------------------- matching

/** Letters and single spaces only — Whisper punctuates and capitalises freely
 *  ("Furi?", "Hey, Furi!"), none of which is part of the phrase. */
function normalize(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z' ]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/** Levenshtein distance, capped: we only ever ask "is this ≤ 1?", so the loop
 *  can stop as soon as the best possible result exceeds the cap. */
function withinDistance(a: string, b: string, max: number): boolean {
  if (a === b) return true;
  if (Math.abs(a.length - b.length) > max) return false;
  // Classic single-row DP; `max` is 1 here so this is trivially small.
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const row = [i];
    let best = i;
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      const value = Math.min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + cost);
      row.push(value);
      if (value < best) best = value;
    }
    if (best > max) return false; // no cell in this row can recover
    prev = row;
  }
  return prev[b.length] <= max;
}

/**
 * A transcript word counts as a phrase word when it is the same word, or one
 * edit away.
 *
 * ⚠️ THE LENGTH GATE IS THE WHOLE SAFETY OF THE FUZZY RULE. At three letters or
 * fewer, one edit reaches a different word entirely ("cat"→"can", "hi"→"hit"), so
 * short words must match exactly. At four or more, one edit is overwhelmingly a
 * mishearing — which is the common case here, because a proper noun no speech
 * model has been trained on comes back as its nearest real word ("Furi" → "fury",
 * "Fury", "furry").
 */
const FUZZY_MIN_LENGTH = 4;

function wordMatches(spoken: string, want: string): boolean {
  if (spoken === want) return true;
  if (want.length < FUZZY_MIN_LENGTH) return false;
  return withinDistance(spoken, want, 1);
}

/**
 * Does this transcript contain the wake phrase?
 *
 * Matches a run of consecutive words anywhere in the transcript, so "Furi",
 * "Hey Furi", and "um, hey Furi, are you there" all fire for a phrase of "furi"
 * or "hey furi" — people do not start a sentence cleanly.
 */
export function matchesWakePhrase(transcript: string, phrase: string): boolean {
  const want = normalize(phrase).split(' ').filter(Boolean);
  if (want.length === 0) return false;
  const said = normalize(transcript).split(' ').filter(Boolean);
  if (said.length < want.length) return false;
  for (let start = 0; start + want.length <= said.length; start++) {
    let all = true;
    for (let k = 0; k < want.length; k++) {
      if (!wordMatches(said[start + k], want[k])) {
        all = false;
        break;
      }
    }
    if (all) return true;
  }
  return false;
}

// ----------------------------------------------------------- WAV encoding

/**
 * Float32 mono samples → a 16-bit PCM WAV blob.
 *
 * The wake capture path produces raw samples from an AudioWorklet, not a
 * MediaRecorder container, so there is nothing to POST without this. WAV rather
 * than opus because it needs no encoder: the backend decodes it with the same
 * PyAV path every other utterance takes.
 */
export function encodeWav(samples: Float32Array, sampleRate: number): Blob {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeText = (offset: number, text: string) => {
    for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
  };
  writeText(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  writeText(8, 'WAVE');
  writeText(12, 'fmt ');
  view.setUint32(16, 16, true); // PCM header size
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate
  view.setUint16(32, 2, true); // block align
  view.setUint16(34, 16, true); // bits per sample
  writeText(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  let offset = 44;
  for (let i = 0; i < samples.length; i++) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, clamped * 0x7fff, true);
    offset += 2;
  }
  return new Blob([buffer], { type: 'audio/wav' });
}

// -------------------------------------------------------------- the gate

/** Raw RMS above which a frame counts as speech. Raw, not voiceInput's ×3.5
 *  display scale — ~0.02 corresponds to that meter reading about 0.07. */
export const SPEECH_RMS = 0.02;
/** Sustained speech needed to open an utterance — rejects clicks and knocks. */
export const SPEECH_OPEN_MS = 120;
/** Quiet needed to close one. Short: a wake phrase is one word. */
export const SPEECH_CLOSE_MS = 450;
/** Audio kept before the gate opened, so the phrase's first consonant survives
 *  the ~120ms it took to notice someone was talking. */
export const PREROLL_MS = 300;
/** Utterance bounds worth transcribing. Below: a noise. Above: someone talking,
 *  not summoning — and skipping those is most of what keeps this affordable. */
export const MIN_UTTERANCE_MS = 300;
export const MAX_UTTERANCE_MS = 3_000;

/**
 * Energy gate over a stream of mono frames: emits each short, complete utterance
 * once it ends. No model, no allocation per frame beyond the ring it keeps.
 */
export class SpeechGate {
  private preroll: Float32Array[] = [];
  private prerollLength = 0;
  private utterance: Float32Array[] = [];
  private utteranceLength = 0;
  private open = false;
  private loudMs = 0;
  private quietMs = 0;
  /**
   * ⚠️ SET AFTER AN OVER-LONG UTTERANCE, AND THE GATE IS MOSTLY POINTLESS
   * WITHOUT IT. Dropping a too-long burst only resets the counters, so the rest
   * of that same sentence immediately re-opens a new utterance and the TAIL gets
   * transcribed — which is the cost this gate exists to avoid, arriving by
   * another door. Someone talking for ten seconds would have produced a wake
   * check every three. Nothing re-opens until real quiet has been observed.
   */
  private waitingForQuiet = false;
  private readonly prerollMax: number;
  private readonly minSamples: number;
  private readonly maxSamples: number;

  constructor(
    private sampleRate: number,
    private onUtterance: (samples: Float32Array) => void
  ) {
    this.prerollMax = Math.round((PREROLL_MS / 1000) * sampleRate);
    this.minSamples = Math.round((MIN_UTTERANCE_MS / 1000) * sampleRate);
    this.maxSamples = Math.round((MAX_UTTERANCE_MS / 1000) * sampleRate);
  }

  push(frame: Float32Array): void {
    if (frame.length === 0) return;
    const frameMs = (frame.length / this.sampleRate) * 1000;
    let sumSquares = 0;
    for (let i = 0; i < frame.length; i++) sumSquares += frame[i] * frame[i];
    const rms = Math.sqrt(sumSquares / frame.length);

    if (rms >= SPEECH_RMS) {
      this.loudMs += frameMs;
      this.quietMs = 0;
    } else {
      this.quietMs += frameMs;
      if (!this.open) this.loudMs = 0; // a stray tick decays, never accumulates
    }

    if (this.waitingForQuiet) {
      if (this.quietMs >= SPEECH_CLOSE_MS) {
        this.waitingForQuiet = false;
        this.loudMs = 0;
      }
      return;
    }

    if (!this.open) {
      this.keepPreroll(frame);
      if (this.loudMs >= SPEECH_OPEN_MS) {
        this.open = true;
        this.utterance = this.preroll;
        this.utteranceLength = this.prerollLength;
        this.preroll = [];
        this.prerollLength = 0;
      }
      return;
    }

    this.utterance.push(frame);
    this.utteranceLength += frame.length;
    // Someone is talking, not summoning — drop it without a transcription, and
    // stay shut until they stop rather than chopping the rest into fragments.
    if (this.utteranceLength > this.maxSamples) {
      this.reset();
      this.waitingForQuiet = true;
      return;
    }
    if (this.quietMs >= SPEECH_CLOSE_MS) {
      const complete = this.utteranceLength >= this.minSamples;
      const samples = complete ? this.flatten() : null;
      this.reset();
      if (samples) this.onUtterance(samples);
    }
  }

  /** Drop everything buffered (the mic became busy, or Jarvis started talking). */
  reset(): void {
    this.preroll = [];
    this.prerollLength = 0;
    this.utterance = [];
    this.utteranceLength = 0;
    this.open = false;
    this.loudMs = 0;
    this.quietMs = 0;
    this.waitingForQuiet = false;
  }

  private keepPreroll(frame: Float32Array): void {
    this.preroll.push(frame);
    this.prerollLength += frame.length;
    while (this.prerollLength > this.prerollMax && this.preroll.length > 1) {
      const dropped = this.preroll.shift();
      this.prerollLength -= dropped ? dropped.length : 0;
    }
  }

  private flatten(): Float32Array {
    const out = new Float32Array(this.utteranceLength);
    let at = 0;
    for (const chunk of this.utterance) {
      out.set(chunk, at);
      at += chunk.length;
    }
    return out;
  }
}
