/**
 * Jarvis OS — Voice Output (Phase 7, Part 4)
 *
 * Sentence-by-sentence speech WHILE the SSE response still streams. The chat
 * route is untouched: chatStore taps its delta stream into this module, an
 * incremental segmenter cuts sentences as they complete, and a sequential
 * playback queue speaks them in order — synthesis of sentence N+1 overlaps
 * playback of sentence N (the latency win). Each sentence itself STREAMS
 * (2026-07-15): /api/voice/speak/stream delivers raw PCM chunks while the
 * sentence is still being synthesized, scheduled gaplessly via Web Audio —
 * audio starts ~1s into synthesis instead of after it; the one-shot blob
 * /speak is the per-sentence fallback. Everything here is best-effort:
 * a failed sentence is skipped, never a stalled queue, never a broken chat
 * turn. Live audio is process state, not render state (the activeRecording
 * precedent) — only a `speaking` flag mirrors into the voice store.
 *
 * Speak policy: a voice-initiated turn always speaks its reply (when output
 * is enabled); `speak_all_responses` covers typed turns. Review-mode
 * transcripts land in the draft and are sent by Enter — those count as typed.
 *
 * Barge-in: pressing the mic, sending a new message, toggling the speaker
 * off, or the stop button all call stopSpeaking() — playback halts and the
 * queue clears instantly (a generation counter invalidates every in-flight
 * async continuation).
 */
import { voiceApi } from '@/lib/api';
import { useVoiceStore } from '@/stores/voiceStore';

/** Don't cut before this many chars — "e.g." / "3.5" / initials never make
 *  a sentence on their own, and micro-clips sound choppy. */
const MIN_SENTENCE_CHARS = 20;

/** First-chunk fast path (TTS latency, 2026-07-15): until a turn's FIRST
 *  utterance is emitted, a clause boundary (, ; :) is also a valid cut past
 *  this length — speech starts on the first clause instead of waiting for a
 *  whole first sentence. */
const FIRST_CLAUSE_MIN_CHARS = 30;
/** …and if no boundary of any kind appeared by this length, soft-cut the
 *  first chunk at the last whitespace so long openers still speak early. */
const FIRST_SOFT_CUT_CHARS = 80;

/** Abbreviations whose trailing period is not a sentence boundary. */
const ABBREVIATION_RE =
  /(?:^|[\s(])(?:e\.g|i\.e|etc|vs|cf|approx|Mr|Mrs|Ms|Dr|Prof|St|Jr|Sr|no)\.$/i;
/** A single capital initial ("J. Smith") is not a sentence boundary. */
const INITIAL_RE = /(?:^|\s)[A-Z]\.$/;

/**
 * Incremental sentence segmenter: feed it deltas, get back completed
 * sentences. A boundary is `.` `!` `?` followed by whitespace already in the
 * buffer (never end-of-buffer — the next delta may continue "3." into "3.5"),
 * or a paragraph break. Never splits inside an open ``` fence, so a whole
 * code block reaches the server in one piece (which speaks one clean
 * "(code omitted)").
 */
export class SentenceSegmenter {
  private buffer = '';
  /** First-chunk fast path: clause/soft cuts only apply before the first
   *  emit; afterwards prosody wins and only real sentences cut. */
  private emittedFirst = false;

  push(delta: string): string[] {
    this.buffer += delta;
    const out: string[] = [];
    for (;;) {
      const cut = this.findBoundary();
      if (cut === -1) break;
      const sentence = this.buffer.slice(0, cut).trim();
      this.buffer = this.buffer.slice(cut);
      if (sentence) {
        out.push(sentence);
        this.emittedFirst = true;
      }
    }
    return out;
  }

  /** The stream is done — whatever remains is the last utterance. */
  flush(): string | null {
    const rest = this.buffer.trim();
    this.buffer = '';
    return rest || null;
  }

  private findBoundary(): number {
    const text = this.buffer;
    let inFence = false;
    let lastSpace = -1;
    for (let i = 0; i < text.length - 1; i++) {
      if (text.startsWith('```', i)) {
        inFence = !inFence;
        i += 2;
        continue;
      }
      if (inFence) continue;
      if (!this.emittedFirst && i <= FIRST_SOFT_CUT_CHARS && /\s/.test(text[i])) {
        lastSpace = i;
      }
      // Paragraph break: always a boundary (a heading or list intro is a
      // natural pause unit even when short).
      if (text[i] === '\n' && text[i + 1] === '\n') {
        return i + 2;
      }
      if ((text[i] === '.' || text[i] === '!' || text[i] === '?')
          && /\s/.test(text[i + 1])) {
        if (i + 1 < MIN_SENTENCE_CHARS) continue;
        const upto = text.slice(0, i + 1);
        if (text[i] === '.' && (ABBREVIATION_RE.test(upto) || INITIAL_RE.test(upto))) {
          continue;
        }
        return i + 1;
      }
      // Clause cut (first chunk only): good-enough prosody, much earlier
      // audio.
      if (!this.emittedFirst
          && (text[i] === ',' || text[i] === ';' || text[i] === ':')
          && /\s/.test(text[i + 1]) && i + 1 >= FIRST_CLAUSE_MIN_CHARS) {
        return i + 1;
      }
    }
    // Soft cut (first chunk only, last resort): a long opener with no
    // boundary of any kind anywhere in the buffer — cut at the last
    // whitespace near the limit so the first words still speak early.
    // Never mid-word, never inside a fence (lastSpace only advances
    // outside fences).
    if (!this.emittedFirst && text.length > FIRST_SOFT_CUT_CHARS
        && lastSpace > MIN_SENTENCE_CHARS) {
      return lastSpace + 1;
    }
    return -1;
  }
}

// ------------------------------------------------------------ module state

/**
 * One sentence's LIVE synthesis (POST /api/voice/speak/stream): starts its
 * fetch immediately (so sentence N+1 synthesizes server-side while N plays —
 * the old blob prefetch, kept) and buffers decoded Float32 PCM chunks until
 * the playback pump drains them. Raw s16le bytes arrive on arbitrary
 * boundaries, so an odd trailing byte carries into the next chunk.
 */
class StreamedUtterance {
  sampleRate = 24000;
  failed: Error | null = null;
  /** 204 — the text sanitized to nothing; skip silently. */
  empty = false;
  settled = false;
  private chunks: Float32Array[] = [];
  private done = false;
  private waiter: ((chunk: Float32Array | null) => void) | null = null;
  private carry: Uint8Array | null = null;

  constructor(private text: string, private signal: AbortSignal) {}

  start(): void {
    void this.run();
  }

  /** Next PCM chunk, or null when the stream is over (single consumer). */
  next(): Promise<Float32Array | null> {
    const queued = this.chunks.shift();
    if (queued) return Promise.resolve(queued);
    if (this.done) return Promise.resolve(null);
    return new Promise((resolve) => { this.waiter = resolve; });
  }

  private async run(): Promise<void> {
    try {
      const res = await voiceApi.speakStream(this.text, this.signal);
      if (!res) {
        this.empty = true;
      } else {
        this.sampleRate = res.sampleRate;
        const reader = res.body.getReader();
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          if (value && value.byteLength) this.push(value);
        }
      }
    } catch (e) {
      this.failed = e instanceof Error ? e : new Error(String(e));
    }
    this.done = true;
    this.settled = true;
    if (this.waiter) {
      const w = this.waiter;
      this.waiter = null;
      w(this.chunks.shift() ?? null);
    }
  }

  private push(bytes: Uint8Array): void {
    let data = bytes;
    if (this.carry) {
      const merged = new Uint8Array(this.carry.length + bytes.length);
      merged.set(this.carry);
      merged.set(bytes, this.carry.length);
      data = merged;
      this.carry = null;
    }
    const usable = data.length - (data.length % 2);
    if (data.length % 2) this.carry = data.slice(usable);
    if (!usable) return;
    // slice() copies → 2-byte alignment guaranteed for the Int16 view.
    const aligned = data.slice(0, usable);
    const ints = new Int16Array(aligned.buffer, 0, usable / 2);
    const floats = new Float32Array(ints.length);
    for (let i = 0; i < ints.length; i++) floats[i] = ints[i] / 32768;
    if (this.waiter) {
      const w = this.waiter;
      this.waiter = null;
      w(floats);
    } else {
      this.chunks.push(floats);
    }
  }
}

interface QueueItem {
  text: string;
  controller: AbortController;
  stream: StreamedUtterance | null;
}

/** Synthesis requests in flight at once — N+1 renders while N plays. */
const MAX_SYNTH_IN_FLIGHT = 2;

let segmenter: SentenceSegmenter | null = null;
let speakingTurn = false;
let nextTurnVoice = false;
let queue: QueueItem[] = [];
let playing = false;
/** The item currently being played — no longer in `queue`, but barge-in must
 *  still abort its stream (or the backend keeps synthesizing a dead
 *  sentence under the synth lock, delaying the next turn's speech). */
let currentItem: QueueItem | null = null;
let currentAudio: HTMLAudioElement | null = null;
let currentUrl: string | null = null;
/** Web Audio scheduling for streamed playback: one lazy shared context, and
 *  the currently scheduled sources so barge-in can silence them instantly. */
let audioCtx: AudioContext | null = null;
const activeSources = new Set<AudioBufferSourceNode>();
/** Bumped by stopSpeaking() — every async continuation checks it and bails
 *  when its world was torn down. */
let generation = 0;

function getAudioContext(): AudioContext {
  if (!audioCtx) audioCtx = new AudioContext();
  if (audioCtx.state === 'suspended') void audioCtx.resume();
  return audioCtx;
}

function setSpeaking(value: boolean) {
  if (useVoiceStore.getState().speaking !== value) {
    useVoiceStore.setState({ speaking: value });
  }
}

// ------------------------------------------------------------- turn policy

/** voiceStore calls this immediately before auto-sending a transcript: the
 *  reply to a spoken question is spoken back. */
export function markNextTurnVoice(): void {
  nextTurnVoice = true;
}

/** A new assistant turn is starting to stream. Decides once whether this
 *  turn speaks; deltas are ignored entirely otherwise. */
export function beginTurn(): void {
  const settings = useVoiceStore.getState().settings;
  const voiceInitiated = nextTurnVoice;
  nextTurnVoice = false;
  speakingTurn =
    !!settings?.enabled &&
    !!settings.output_enabled &&
    (voiceInitiated || !!settings.speak_all_responses);
  segmenter = speakingTurn ? new SentenceSegmenter() : null;
}

/** One streamed delta (the single chatStore tap). Plan chunks never reach
 *  this — the plan branch returns before the delta append. */
export function onDelta(delta: string): void {
  if (!speakingTurn || !segmenter || !delta) return;
  for (const sentence of segmenter.push(delta)) {
    enqueue(sentence);
  }
}

/** The stream finished — speak the remainder. */
export function endTurn(): void {
  if (speakingTurn && segmenter) {
    const rest = segmenter.flush();
    if (rest) enqueue(rest);
  }
  speakingTurn = false;
  segmenter = null;
}

/** The stream errored — stop queueing new sentences; anything already
 *  queued/playing finishes (it was real, delivered text). */
export function cancelTurn(): void {
  speakingTurn = false;
  segmenter = null;
}

/** The server's /speak limit — a longer request would 400 and be skipped. */
const MAX_SPEAK_CHARS = 2000;

/**
 * Speak one standalone utterance (Part 5 — proactive announcements). Rides
 * the SAME queue as streamed responses, so an announcement serializes behind
 * a reply being spoken and dies on every existing barge-in (mic press, new
 * message, speaker off, stop button). Policy gating (speak_proactive,
 * silent types, mic-open) lives with the caller — this just speaks.
 */
export function speakText(text: string): void {
  const trimmed = text.trim();
  if (!trimmed) return;
  enqueue(trimmed.slice(0, MAX_SPEAK_CHARS));
}

// ------------------------------------------------- queue + playback engine

function enqueue(text: string): void {
  queue.push({ text, controller: new AbortController(), stream: null });
  pumpSynthesis();
  void pumpPlayback();
}

function startSynthesis(item: QueueItem): void {
  if (item.stream) return;
  item.stream = new StreamedUtterance(item.text, item.controller.signal);
  item.stream.start();
}

/** Keep up to MAX_SYNTH_IN_FLIGHT synthesis requests running, in order. */
function pumpSynthesis(): void {
  let inFlight = 0;
  for (const item of queue) {
    if (item.stream && !item.stream.settled) inFlight++;
  }
  for (const item of queue) {
    if (inFlight >= MAX_SYNTH_IN_FLIGHT) break;
    if (item.stream) continue;
    startSynthesis(item);
    inFlight++;
  }
}

/**
 * Schedule a streamed utterance's PCM chunks gaplessly on the shared
 * AudioContext as they arrive — audio starts on the FIRST chunk, ~1s into
 * synthesis, instead of after the whole sentence. Resolves when the last
 * scheduled sample has finished playing. Throws only when the stream failed
 * before ANY audio was scheduled (the caller falls back to the blob path).
 */
async function playStream(stream: StreamedUtterance, gen: number): Promise<void> {
  const ctx = getAudioContext();
  let nextStart = 0;
  let scheduledAny = false;
  let lastEnded: Promise<void> = Promise.resolve();
  for (;;) {
    const chunk = await stream.next();
    if (gen !== generation) return; // barge-in while synthesizing
    if (!chunk) break;
    if (!chunk.length) continue;
    const buffer = ctx.createBuffer(1, chunk.length, stream.sampleRate);
    buffer.getChannelData(0).set(chunk);
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);
    // A hair of scheduling headroom before the first chunk; later chunks
    // butt-join (or restart "now" after a production underrun).
    const startAt = Math.max(nextStart, ctx.currentTime + (scheduledAny ? 0 : 0.06));
    nextStart = startAt + buffer.duration;
    activeSources.add(source);
    lastEnded = new Promise<void>((resolve) => {
      source.onended = () => {
        activeSources.delete(source);
        resolve();
      };
    });
    source.start(startAt);
    if (!scheduledAny) {
      scheduledAny = true;
      setSpeaking(true);
    }
  }
  if (!scheduledAny) {
    if (stream.failed) throw stream.failed;
    return; // empty (204) — nothing to play
  }
  // Sources end in order; the last one's onended is the utterance's end
  // (barge-in stop() also fires onended, so this never dangles).
  await lastEnded;
}

/** Play queued sentences strictly in order, one at a time. */
async function pumpPlayback(): Promise<void> {
  if (playing) return;
  const item = queue.shift();
  if (!item) {
    setSpeaking(false);
    return;
  }
  playing = true;
  currentItem = item;
  const gen = generation;
  // The head item may not have started yet (only MAX_SYNTH_IN_FLIGHT run at
  // once, and it just left the queue the pump scans) — start it directly.
  startSynthesis(item);
  pumpSynthesis();
  try {
    try {
      await playStream(item.stream!, gen);
    } catch {
      // The stream failed before any audio (endpoint missing/erroring) —
      // fall back to the one-shot blob path for this sentence.
      if (gen !== generation || item.controller.signal.aborted) return;
      const blob = await voiceApi.speak(item.text, item.controller.signal);
      if (gen !== generation) return;
      if (blob) {
        setSpeaking(true);
        await playBlob(blob, gen);
      }
    }
  } catch {
    // Synthesis failed/aborted — skip this sentence, never stall the queue.
  } finally {
    if (currentItem === item) currentItem = null;
    if (gen === generation) {
      playing = false;
      void pumpPlayback();
    }
  }
}

function playBlob(blob: Blob, gen: number): Promise<void> {
  return new Promise((resolve) => {
    if (gen !== generation) return resolve();
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    currentAudio = audio;
    currentUrl = url;
    const finish = () => {
      if (currentUrl === url) {
        URL.revokeObjectURL(url);
        currentAudio = null;
        currentUrl = null;
      }
      resolve();
    };
    audio.onended = finish;
    audio.onerror = finish;
    audio.play().catch(finish);
  });
}

/**
 * Barge-in: halt playback and clear the queue INSTANTLY. Also stops the
 * current turn from queueing anything further.
 */
export function stopSpeaking(): void {
  generation++;
  speakingTurn = false;
  segmenter = null;
  for (const item of queue) item.controller.abort();
  if (currentItem) currentItem.controller.abort();
  currentItem = null;
  queue = [];
  playing = false;
  // Streamed playback: silence every scheduled Web Audio source instantly
  // (stop() fires onended, so no playStream await ever dangles).
  for (const source of activeSources) {
    try {
      source.stop();
    } catch { /* already ended */ }
  }
  activeSources.clear();
  if (currentAudio) {
    currentAudio.pause();
    currentAudio.src = '';
  }
  if (currentUrl) URL.revokeObjectURL(currentUrl);
  currentAudio = null;
  currentUrl = null;
  setSpeaking(false);
}
