/**
 * Jarvis OS — Wake Word "Hey Jarvis" (Phase 12.2)
 *
 * Fully ON-DEVICE wake-word detection. An always-on 16 kHz mic stream is
 * consumed by three tiny local ONNX models (openWakeWord's pretrained
 * melspectrogram → embedding → hey_jarvis classifier, Apache-2.0) running in
 * the renderer via onnxruntime-web. Raw detection audio NEVER leaves the
 * machine — only the post-wake utterance is posted to the loopback STT, exactly
 * like the hotkey path. On a score over threshold we fire
 * voiceStore.beginWakeListen(), which reuses the existing hands-free capture
 * pipeline (review/speak rules + continuous conversation all apply).
 *
 * Structural gates (the caller keys start/stop on enabled && wake_word):
 * - Detection is SUSPENDED whenever the mic is otherwise in use (phase !==
 *   'idle') or Jarvis is speaking — prevents self-trigger and mic contention;
 *   buffers reset on suspend so no stale audio triggers on resume.
 * - A cooldown after each trigger avoids double-fires.
 *
 * Everything is best-effort: a model-load or inference failure logs once and
 * degrades to "no wake word", never a crash or a broken chat turn.
 *
 * NOTE (live-tuning): the streaming frame arithmetic follows openWakeWord's
 * reference (80 ms chunks → 8 mel frames; 76-frame embedding window; 16
 * embeddings per prediction). THRESHOLD is conservative and may want tuning
 * against the real model on this hardware — see the plan's verification step.
 */
import type * as Ort from 'onnxruntime-web';
import { useVoiceStore } from '@/stores/voiceStore';
import melspecUrl from '@/assets/wakeword/melspectrogram.onnx?url';
import embeddingUrl from '@/assets/wakeword/embedding_model.onnx?url';
import wakewordUrl from '@/assets/wakeword/hey_jarvis_v0.1.onnx?url';
// The onnxruntime-web runtime binary is resolved by Vite: the default "bundle"
// build references its .wasm via `new URL(..., import.meta.url)`, which Vite
// rewrites to a hashed asset URL that resolves in both dev (http://) and
// packaged Electron (file://). So we do NOT override ort.env.wasm.wasmPaths —
// the package's `exports` map blocks importing the .wasm as a module anyway.
// DEV GOTCHA (live failure 2026-07-16): that URL rewrite does NOT happen
// inside a Vite-PRE-BUNDLED dep — the .wasm request resolved into
// .vite/deps/, got index.html back, and WASM compile aborted on the "<!do"
// magic word, silently killing the wake word. vite.config.ts therefore
// carries `optimizeDeps.exclude: ['onnxruntime-web']`; keep it there.

// ---- openWakeWord streaming constants
const SAMPLE_RATE = 16_000;
const CHUNK = 1_280; // 80 ms of audio per processing step
const MEL_LOOKBACK = 480; // 3 hops of context so the newest mel frames are correct
const MEL_STEP = 8; // new mel frames produced per 80 ms chunk
const MEL_WINDOW = 76; // mel frames consumed per embedding
const WAKE_FRAMES = 16; // embeddings per wake-word prediction
const EMBEDDING_DIM = 96;
const MEL_BINS = 32;

// ---- detection tuning
const THRESHOLD = 0.5;
const COOLDOWN_MS = 3_000;

// ---- real-time backpressure
// If the three-model chain can't keep up with the mic, unconsumed samples pile
// up in `pending`. Unbounded, that backlog (plus the per-chunk slice churn
// over an ever-growing array) grew until the renderer was OOM-killed — blank
// window, DevTools disconnected (live failure 2026-07-16, the first day this
// path actually ran; the WASM load failure had masked it). Wake detection
// only needs the last ~1.3s of audio, so a backlog covering seconds means the
// CPU is simply too slow: drop the buffered audio, and after repeated
// overflows stop entirely — honest degradation beats a pegged CPU and a dead
// renderer.
const MAX_BACKLOG_SAMPLES = SAMPLE_RATE * 3; // 3s of unprocessed audio
const MAX_OVERFLOWS = 5;

// ---- crash-loop breaker
// The renderer died repeatedly right after wake-word start (live failure
// 2026-07-16: blank→reload→blank cycles until the main-process reload cap),
// and nothing inside a renderer can catch its own process death. So the
// module keeps a strike counter in localStorage: a strike is written when
// detection starts, and cleared only on a GRACEFUL outcome — stopWakeWord()
// or 30s of stable running. A crash can never clear it. Two fresh strikes =
// wake word is what's killing this renderer → skip starting it (console-
// warned) so the reload lands on a usable app. Strikes go stale after 10
// minutes, so the feature retries on a later launch (self-healing; the cost
// of a wrong strike — e.g. two rapid quit-after-launch cycles — is 10 wake-
// word-less minutes, never a broken app).
const CRASH_GUARD_KEY = 'jarvis.wakeword.crash-strikes';
const CRASH_GUARD_LIMIT = 2;
const CRASH_GUARD_STABLE_MS = 30_000;
const CRASH_GUARD_FRESH_MS = 10 * 60_000;

// ------------------------------------------------------------- module state
let running = false;
let suspended = false;
let ort: typeof Ort | null = null;
let melSession: Ort.InferenceSession | null = null;
let embedSession: Ort.InferenceSession | null = null;
let wakeSession: Ort.InferenceSession | null = null;

let stream: MediaStream | null = null;
let audioCtx: AudioContext | null = null;
let processor: ScriptProcessorNode | null = null;
let sink: GainNode | null = null;
let unsubStore: (() => void) | null = null;

/** Incoming 16 kHz samples awaiting processing (drained in CHUNK-sized steps). */
let pending: number[] = [];
let processingQueue = false;
let prevTail = new Float32Array(MEL_LOOKBACK); // lookback context for the mel window
let melBuffer: Float32Array[] = []; // rolling mel frames (each MEL_BINS long)
let featureBuffer: Float32Array[] = []; // rolling embeddings (each EMBEDDING_DIM long)
let lastTriggerAt = 0;
let overflowCount = 0; // per-session; see MAX_OVERFLOWS
let crashGuardTimer: number | null = null;
let inferredOnce = false; // first full inference chain completed (see noteStage)


function readStrikes(): { strikes: number; at: number; stage?: string } {
  try {
    const raw = localStorage.getItem(CRASH_GUARD_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as {
        strikes?: number;
        at?: number;
        stage?: string;
      };
      return { strikes: parsed.strikes ?? 0, at: parsed.at ?? 0, stage: parsed.stage };
    }
  } catch {
    // Unreadable storage = no strikes; the guard is best-effort.
  }
  return { strikes: 0, at: 0 };
}

/** Record how far startup got on the CURRENT strike — a crash freezes the
 *  last stage written, so the breaker (and the ~/.jarvis crash log's
 *  timestamps) can say WHERE the renderer died: loading models, opening the
 *  mic, or only once real inference began. Forensics only; best-effort. */
function noteStage(stage: string): void {
  try {
    const raw = localStorage.getItem(CRASH_GUARD_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    parsed.stage = stage;
    localStorage.setItem(CRASH_GUARD_KEY, JSON.stringify(parsed));
  } catch {
    // Best-effort.
  }
}

function clearStrikes(): void {
  if (crashGuardTimer !== null) {
    window.clearTimeout(crashGuardTimer);
    crashGuardTimer = null;
  }
  try {
    localStorage.removeItem(CRASH_GUARD_KEY);
  } catch {
    // Best-effort.
  }
}

// ------------------------------------------------------------- lifecycle

/** Start always-on wake-word detection. Idempotent; safe to call when already
 *  running. Best-effort — any failure logs and leaves wake word inactive. */
export async function startWakeWord(): Promise<void> {
  if (running) return;
  const guard = readStrikes();
  if (
    guard.strikes >= CRASH_GUARD_LIMIT &&
    Date.now() - guard.at < CRASH_GUARD_FRESH_MS
  ) {
    console.warn(
      '[WakeWord] not starting: the previous renderer sessions crashed right ' +
        `after wake-word start (crash-loop breaker; last stage reached: ` +
        `${guard.stage ?? 'unknown'}). Will retry on a later launch.`
    );
    return;
  }
  try {
    localStorage.setItem(
      CRASH_GUARD_KEY,
      JSON.stringify({ strikes: guard.strikes + 1, at: Date.now(), stage: 'starting' })
    );
  } catch {
    // Best-effort.
  }
  running = true;
  overflowCount = 0;
  inferredOnce = false;
  try {
    noteStage('loading-models');
    await ensureModels();
    noteStage('opening-mic');
    await openMic();
    subscribeSuspend();
    noteStage('running');
    // Survived startup: after a stable window the strike is forgiven.
    crashGuardTimer = window.setTimeout(clearStrikes, CRASH_GUARD_STABLE_MS);
  } catch (e) {
    console.warn('[WakeWord] failed to start; wake word inactive:', e);
    stopWakeWord();
  }
}

/** Stop detection and release the mic. Safe to call any time. */
export function stopWakeWord(): void {
  // A graceful stop of a LIVE session is not a crash. Guarded on `running`
  // because App.tsx calls this on mount before settings load — an
  // unconditional clear would wipe the strikes before startWakeWord ever
  // reads them and the breaker could never trip.
  if (running) clearStrikes();
  running = false;
  suspended = false;
  if (unsubStore) {
    unsubStore();
    unsubStore = null;
  }
  closeMic();
  resetBuffers();
}

// ------------------------------------------------------------- model loading

async function ensureModels(): Promise<void> {
  if (melSession && embedSession && wakeSession) return;
  ort = await import('onnxruntime-web');
  ort.env.wasm.numThreads = 1; // renderer: keep it light, no cross-origin isolation needed
  const opts: Ort.InferenceSession.SessionOptions = {
    executionProviders: ['wasm'],
    graphOptimizationLevel: 'all',
  };
  // Sequential, not Promise.all: three concurrent session builds spike the
  // wasm-heap allocation at the exact moment the renderer has been dying
  // (2026-07-16 crash rounds). Serial costs ~nothing at startup and keeps
  // the peak flat.
  melSession = await ort.InferenceSession.create(melspecUrl, opts);
  embedSession = await ort.InferenceSession.create(embeddingUrl, opts);
  wakeSession = await ort.InferenceSession.create(wakewordUrl, opts);
}

// ------------------------------------------------------------- mic capture

async function openMic(): Promise<void> {
  stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true },
  });
  // A dedicated 16 kHz context resamples the mic to the rate the models expect.
  audioCtx = new AudioContext({ sampleRate: SAMPLE_RATE });
  if (audioCtx.state === 'suspended') await audioCtx.resume();
  const source = audioCtx.createMediaStreamSource(stream);
  // ScriptProcessor is deprecated but simplest + reliable in Electron; a
  // zero-gain sink keeps it running without routing the mic to the speakers.
  processor = audioCtx.createScriptProcessor(4096, 1, 1);
  sink = audioCtx.createGain();
  sink.gain.value = 0;
  processor.onaudioprocess = (e: AudioProcessingEvent) => {
    if (!running || suspended) return;
    const input = e.inputBuffer.getChannelData(0);
    for (let i = 0; i < input.length; i++) pending.push(input[i]);
    if (pending.length > MAX_BACKLOG_SAMPLES) {
      handleOverflow();
      return;
    }
    void drainQueue();
  };
  source.connect(processor);
  processor.connect(sink);
  sink.connect(audioCtx.destination);
}

function closeMic(): void {
  if (processor) {
    processor.onaudioprocess = null;
    try { processor.disconnect(); } catch { /* already gone */ }
    processor = null;
  }
  if (sink) {
    try { sink.disconnect(); } catch { /* already gone */ }
    sink = null;
  }
  if (audioCtx) {
    void audioCtx.close().catch(() => undefined);
    audioCtx = null;
  }
  if (stream) {
    stream.getTracks().forEach((t) => t.stop());
    stream = null;
  }
}

// ------------------------------------------------------------- suspend/resume

/** Suspend while the mic is otherwise busy or Jarvis is speaking; resume on a
 *  return to idle. Reset buffers on suspend so no stale audio triggers later. */
function subscribeSuspend(): void {
  const evaluate = () => {
    const v = useVoiceStore.getState();
    const shouldSuspend = v.phase !== 'idle' || v.speaking;
    if (shouldSuspend && !suspended) {
      suspended = true;
      resetBuffers();
    } else if (!shouldSuspend && suspended) {
      suspended = false;
    }
  };
  evaluate();
  unsubStore = useVoiceStore.subscribe(evaluate);
}

function resetBuffers(): void {
  pending = [];
  prevTail = new Float32Array(MEL_LOOKBACK);
  melBuffer = [];
  featureBuffer = [];
}

/** Inference fell seconds behind the mic (see MAX_BACKLOG_SAMPLES). Drop the
 *  backlog; after repeated overflows this machine demonstrably can't run wake
 *  word in real time — stop it for the session rather than melt the CPU. */
function handleOverflow(): void {
  overflowCount += 1;
  resetBuffers();
  if (overflowCount >= MAX_OVERFLOWS) {
    console.warn(
      '[WakeWord] inference cannot keep up with real-time audio on this machine; ' +
        'disabling wake word for this session.'
    );
    stopWakeWord();
  } else {
    console.warn(
      `[WakeWord] audio backlog overflowed (${overflowCount}/${MAX_OVERFLOWS}) — dropping buffered audio.`
    );
  }
}

// ------------------------------------------------------------- inference loop

/** Drain pending audio in CHUNK-sized steps, single-in-flight so slow inference
 *  self-paces (the interim-transcription discipline). */
async function drainQueue(): Promise<void> {
  if (processingQueue) return;
  processingQueue = true;
  try {
    while (running && !suspended && pending.length >= CHUNK) {
      const chunk = Float32Array.from(pending.slice(0, CHUNK));
      pending = pending.slice(CHUNK);
      await processChunk(chunk);
      if (!inferredOnce) {
        inferredOnce = true;
        noteStage('inference-ok');
      }
    }
  } catch (e) {
    console.warn('[WakeWord] inference error (ignored):', e);
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
      handleScore(scoreOut[0] ?? 0);
    }
  }
}

/** Run one session with a single float32 input tensor; return the first
 *  output's data. Input/output names are read from the session so the models'
 *  exact names don't have to be hard-coded. */
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

function handleScore(score: number): void {
  if (score < THRESHOLD) return;
  const now = Date.now();
  if (now - lastTriggerAt < COOLDOWN_MS) return;
  lastTriggerAt = now;
  resetBuffers(); // don't re-fire on the tail of the same utterance
  void useVoiceStore.getState().beginWakeListen();
}
