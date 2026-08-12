/**
 * Furi OS — Wake Word inference worker (Phase 12.2, hardened 2026-07-16)
 *
 * Runs openWakeWord's three ONNX models (melspectrogram → embedding →
 * hey_jarvis) via onnxruntime-web, OFF the renderer's main/audio thread. This
 * worker owns everything heavy: the three sessions, the rolling mel/embedding
 * buffers, and the CHUNK-stepped inference. The main thread (wakeWord.ts) only
 * captures the mic and forwards raw 16 kHz samples here, then reacts to the
 * scores this worker posts back.
 *
 * WHY A WORKER + WHY THE ORT CONFIG (the 0xC0000005 fix):
 * onnxruntime-web >=1.19 ships ONLY the threaded/shared-memory wasm build, which
 * needs cross-origin isolation (SharedArrayBuffer) to run. The renderer is not
 * isolated, so that build executed kernels in a broken memory state and the
 * renderer died with a native ACCESS VIOLATION (exitCode 0xC0000005) the instant
 * the first session.run() fired. Fix: pin onnxruntime-web to a version that
 * ships a non-threaded build (1.17.3 → ort-wasm-simd.wasm / ort-wasm.wasm),
 * force numThreads=1 + proxy=false, and point wasmPaths straight at those
 * self-hosted, non-shared binaries (URLs resolved by Vite on the main thread and
 * passed in on init) — no SharedArrayBuffer, no cross-origin isolation needed.
 * Running in a worker also keeps the always-on audio callback contention-free
 * and makes any ORT error catchable (worker.onerror) instead of a silent
 * renderer death.
 */
import type * as Ort from 'onnxruntime-web';

// ---- openWakeWord streaming constants (verbatim — verified against the real
// models: 1760 samples → 8 mel frames; 76-frame window → 96-dim embedding; 16
// embeddings → wake score. Do NOT change this arithmetic.)
const SAMPLE_RATE = 16_000;
const CHUNK = 1_280; // 80 ms of audio per processing step
const MEL_LOOKBACK = 480; // 3 hops of context so the newest mel frames are correct
const MEL_STEP = 8; // new mel frames produced per 80 ms chunk
const MEL_WINDOW = 76; // mel frames consumed per embedding
const WAKE_FRAMES = 16; // embeddings per wake-word prediction
const EMBEDDING_DIM = 96;
const MEL_BINS = 32;

// ---- real-time backpressure (owned here, off the audio thread). If the model
// chain can't keep up, `pending` grows; unbounded that once OOM-killed the
// renderer. Wake detection only needs ~1.3s of audio, so a multi-second backlog
// means this CPU is simply too slow: drop it, and after repeated overflows stop.
const MAX_BACKLOG_SAMPLES = SAMPLE_RATE * 3; // 3s of unprocessed audio
const MAX_OVERFLOWS = 5;

// ------------------------------------------------------------- messages
interface InitMsg {
  type: 'init';
  melUrl: string;
  embedUrl: string;
  wakeUrl: string;
  wasmSimdUrl: string;
  wasmUrl: string;
  /** Fallback rung: force the plain scalar wasm (disable SIMD) if a SIMD build
   *  still faults on this hardware. */
  disableSimd?: boolean;
}
interface AudioMsg {
  type: 'audio';
  buf: Float32Array;
}
interface ResetMsg {
  type: 'reset';
}
type InMsg = InitMsg | AudioMsg | ResetMsg;

// ------------------------------------------------------------- state
let ort: typeof Ort | null = null;
let melSession: Ort.InferenceSession | null = null;
let embedSession: Ort.InferenceSession | null = null;
let wakeSession: Ort.InferenceSession | null = null;

let pending: number[] = [];
let processingQueue = false;
let prevTail = new Float32Array(MEL_LOOKBACK);
let melBuffer: Float32Array[] = [];
let featureBuffer: Float32Array[] = [];
let overflowCount = 0;
let ready = false;

const ctx = self as unknown as Worker;
function post(msg: unknown): void {
  ctx.postMessage(msg);
}

// ------------------------------------------------------------- init
async function init(m: InitMsg): Promise<void> {
  ort = await import('onnxruntime-web');
  // Single-threaded, non-shared wasm: the whole point of the fix.
  ort.env.wasm.numThreads = 1;
  ort.env.wasm.proxy = false;
  if (m.disableSimd) ort.env.wasm.simd = false;
  // Silence ORT's benign model-load warnings (e.g. "Removing initializer … not
  // used by any node") — they print to stderr and show as red console errors.
  ort.env.logLevel = 'error';
  // Explicit, self-hosted wasm — pins the exact non-threaded binary and bypasses
  // ORT's internal `new URL(...wasm, import.meta.url)` resolution entirely.
  ort.env.wasm.wasmPaths = {
    'ort-wasm-simd.wasm': m.wasmSimdUrl,
    'ort-wasm.wasm': m.wasmUrl,
  };
  const opts: Ort.InferenceSession.SessionOptions = {
    executionProviders: ['wasm'],
    graphOptimizationLevel: 'all',
    logSeverityLevel: 3, // errors only — suppress per-session load warnings
  };
  // Sequential, not Promise.all: three concurrent builds spike the wasm heap.
  melSession = await ort.InferenceSession.create(m.melUrl, opts);
  embedSession = await ort.InferenceSession.create(m.embedUrl, opts);
  wakeSession = await ort.InferenceSession.create(m.wakeUrl, opts);
  ready = true;
  post({ type: 'ready' });
}

function resetBuffers(): void {
  pending = [];
  prevTail = new Float32Array(MEL_LOOKBACK);
  melBuffer = [];
  featureBuffer = [];
}

function handleOverflow(): void {
  overflowCount += 1;
  resetBuffers();
  const fatal = overflowCount >= MAX_OVERFLOWS;
  post({ type: 'overflow', count: overflowCount, max: MAX_OVERFLOWS, fatal });
}

// ------------------------------------------------------------- inference loop
async function drainQueue(): Promise<void> {
  if (processingQueue) return;
  processingQueue = true;
  try {
    while (ready && pending.length >= CHUNK) {
      const chunk = Float32Array.from(pending.slice(0, CHUNK));
      pending = pending.slice(CHUNK);
      await processChunk(chunk);
    }
  } catch (e) {
    post({ type: 'error', message: `inference: ${(e as Error)?.message ?? e}` });
  } finally {
    processingQueue = false;
  }
}

async function processChunk(chunk: Float32Array): Promise<void> {
  if (!ort || !melSession || !embedSession || !wakeSession) return;

  // 1) Mel spectrogram over [lookback | chunk]; keep the newest MEL_STEP frames.
  const melInput = new Float32Array(MEL_LOOKBACK + CHUNK);
  melInput.set(prevTail, 0);
  melInput.set(chunk, MEL_LOOKBACK);
  prevTail = chunk.slice(CHUNK - MEL_LOOKBACK);

  const melOut = await run(melSession, melInput, [1, melInput.length]);
  const frameCount = Math.floor(melOut.length / MEL_BINS);
  // openWakeWord normalizes the raw mel: x/10 + 2.
  const startFrame = Math.max(0, frameCount - MEL_STEP);
  for (let f = startFrame; f < frameCount; f++) {
    const frame = new Float32Array(MEL_BINS);
    for (let m = 0; m < MEL_BINS; m++) frame[m] = melOut[f * MEL_BINS + m] / 10 + 2;
    melBuffer.push(frame);
  }

  // 2) Embeddings: one per MEL_STEP new frames over a MEL_WINDOW window.
  while (melBuffer.length >= MEL_WINDOW) {
    const windowData = new Float32Array(MEL_WINDOW * MEL_BINS);
    for (let i = 0; i < MEL_WINDOW; i++) windowData.set(melBuffer[i], i * MEL_BINS);
    melBuffer.splice(0, MEL_STEP);

    const embOut = await run(embedSession, windowData, [1, MEL_WINDOW, MEL_BINS, 1]);
    featureBuffer.push(Float32Array.from(embOut.slice(0, EMBEDDING_DIM)));
    if (featureBuffer.length > WAKE_FRAMES) featureBuffer.shift();

    // 3) Wake-word score over the last WAKE_FRAMES embeddings.
    if (featureBuffer.length === WAKE_FRAMES) {
      const feats = new Float32Array(WAKE_FRAMES * EMBEDDING_DIM);
      for (let i = 0; i < WAKE_FRAMES; i++) feats.set(featureBuffer[i], i * EMBEDDING_DIM);
      const scoreOut = await run(wakeSession, feats, [1, WAKE_FRAMES, EMBEDDING_DIM]);
      post({ type: 'score', value: scoreOut[0] ?? 0 });
    }
  }
}

/** Run one session with a single float32 input tensor; return the first
 *  output's data. Names are read from the session so exact model names aren't
 *  hard-coded. */
async function run(
  session: Ort.InferenceSession,
  data: Float32Array,
  dims: number[]
): Promise<Float32Array> {
  const tensor = new ort!.Tensor('float32', data, dims);
  const feeds: Record<string, Ort.Tensor> = { [session.inputNames[0]]: tensor };
  const out = await session.run(feeds);
  return out[session.outputNames[0]].data as Float32Array;
}

// ------------------------------------------------------------- message pump
ctx.onmessage = (e: MessageEvent<InMsg>) => {
  const msg = e.data;
  if (msg.type === 'init') {
    init(msg).catch((err) =>
      post({ type: 'error', message: `init: ${(err as Error)?.message ?? err}` })
    );
    return;
  }
  if (msg.type === 'reset') {
    resetBuffers();
    return;
  }
  if (msg.type === 'audio') {
    if (!ready) return;
    const input = msg.buf;
    for (let i = 0; i < input.length; i++) pending.push(input[i]);
    if (pending.length > MAX_BACKLOG_SAMPLES) {
      handleOverflow();
      return;
    }
    void drainQueue();
  }
};
