/**
 * Jarvis OS — Wake Word "Hey Jarvis" (Phase 12.2)
 *
 * Fully ON-DEVICE wake-word detection. An always-on 16 kHz mic stream is
 * captured on the main thread and forwarded to a Web Worker (wakeWorker.ts),
 * which runs three tiny local ONNX models (openWakeWord's pretrained
 * melspectrogram → embedding → hey_jarvis classifier, Apache-2.0) via
 * onnxruntime-web. Raw detection audio NEVER leaves the machine — only the
 * post-wake utterance is posted to the loopback STT, exactly like the hotkey
 * path. On a score over threshold we fire voiceStore.beginWakeListen(), which
 * reuses the existing hands-free capture pipeline (review/speak rules +
 * continuous conversation all apply).
 *
 * ROOT-CAUSE HISTORY (2026-07-16): the inference used to run on the renderer's
 * main thread against onnxruntime-web 1.19.2, whose ONLY wasm build is
 * threaded/shared-memory and needs cross-origin isolation the renderer lacks —
 * so session.run() faulted natively and killed the renderer (blank window,
 * exitCode 0xC0000005). Fixed by pinning onnxruntime-web to 1.17.3 and running a
 * single-threaded, non-shared wasm inside a worker with explicit wasmPaths — see
 * wakeWorker.ts for the full explanation. The worker also de-contends the audio
 * thread and makes ORT errors catchable instead of a silent process death.
 *
 * Structural gates (the caller keys start/stop on enabled && wake_word):
 * - Detection is SUSPENDED whenever the mic is otherwise in use (phase !==
 *   'idle') or Jarvis is speaking — prevents self-trigger and mic contention;
 *   the worker's buffers reset on suspend so no stale audio triggers on resume.
 * - A cooldown after each trigger avoids double-fires.
 *
 * Everything is best-effort: a model-load or inference failure logs once and
 * degrades to "no wake word", never a crash or a broken chat turn.
 */
import { useVoiceStore } from '@/stores/voiceStore';
import melspecUrl from '@/assets/wakeword/melspectrogram.onnx?url';
import embeddingUrl from '@/assets/wakeword/embedding_model.onnx?url';
import wakewordUrl from '@/assets/wakeword/hey_jarvis_v0.1.onnx?url';
// Non-threaded, non-shared wasm binaries, self-hosted. Copied from
// onnxruntime-web/dist into src/assets/ort (the package's `exports` map blocks
// deep-importing its .wasm directly). Vite resolves these to hashed asset URLs
// that work in dev (http://) and packaged Electron (file://), exactly like the
// .onnx models above; they are handed to the worker, which points
// ort.env.wasm.wasmPaths at them. This is the fix: NOT the shared-memory 1.19
// build, and no cross-origin isolation required. See wakeWorker.ts.
import ortWasmSimdUrl from '@/assets/ort/ort-wasm-simd.wasm?url';
import ortWasmUrl from '@/assets/ort/ort-wasm.wasm?url';

// ---- audio capture
// TARGET_RATE is what the models expect. We DELIBERATELY do NOT force the
// AudioContext to this rate: `new AudioContext({ sampleRate: 16000 })` + a
// deprecated ScriptProcessorNode was the whole crash — it faulted the renderer
// natively (0xC0000005) on this machine, proven by an audio-only diagnostic that
// crashed with ZERO onnx loaded. Instead we run a native-rate context + an
// AudioWorklet (the shape STT uses safely) and resample to 16 kHz in JS.
const TARGET_RATE = 16_000;

// ---- detection tuning
const THRESHOLD = 0.5;
const COOLDOWN_MS = 3_000;

// ---- crash-loop breaker (safety net; the worker/wasm fix should mean it never
// trips now). The renderer once died repeatedly right after wake-word start and
// nothing inside a renderer can catch its own process death. The module keeps a
// strike counter in localStorage: a strike is written when detection starts, and
// cleared only on a GRACEFUL outcome — stopWakeWord() or 30s of stable running.
// A crash can never clear it. Two fresh strikes = wake word is what's killing
// this renderer → skip starting it so the reload lands on a usable app. Strikes
// go stale after 10 minutes, so the feature retries on a later launch.
const CRASH_GUARD_KEY = 'jarvis.wakeword.crash-strikes';
const CRASH_GUARD_LIMIT = 2;
const CRASH_GUARD_STABLE_MS = 30_000;
const CRASH_GUARD_FRESH_MS = 10 * 60_000;

// ------------------------------------------------------------- module state
let running = false;
let suspended = false;
let worker: Worker | null = null;

let stream: MediaStream | null = null;
let audioCtx: AudioContext | null = null;
let workletNode: AudioWorkletNode | null = null;
let sink: GainNode | null = null;
let unsubStore: (() => void) | null = null;

let lastTriggerAt = 0;
let crashGuardTimer: number | null = null;
let inferredOnce = false; // first score received from the worker (see noteStage)

// ---- streaming linear resampler: native context rate → TARGET_RATE. State
// persists across worklet frames so there's no discontinuity at chunk seams.
let resampleStep = 1; // native samples per output sample (nativeRate / TARGET_RATE)
let resampleT = 0; // fractional read cursor into the current [prev | chunk]
let resamplePrev = 0; // last sample of the previous native-rate chunk
let workletUrl: string | null = null; // cached Blob URL for the inline worklet

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

/** Record how far startup got on the CURRENT strike — a crash freezes the last
 *  stage written, so the breaker can say WHERE the renderer died: loading
 *  models, opening the mic, or only once real inference began. Best-effort. */
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
  suspended = false;
  inferredOnce = false;
  try {
    noteStage('loading-models');
    await startWorker();
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

/** Stop detection, terminate the worker, and release the mic. Safe any time. */
export function stopWakeWord(): void {
  // A graceful stop of a LIVE session is not a crash. Guarded on `running`
  // because App.tsx calls this on mount before settings load — an unconditional
  // clear would wipe the strikes before startWakeWord ever reads them and the
  // breaker could never trip.
  if (running) clearStrikes();
  running = false;
  suspended = false;
  if (unsubStore) {
    unsubStore();
    unsubStore = null;
  }
  closeMic();
  if (worker) {
    worker.onmessage = null;
    worker.onerror = null;
    worker.terminate();
    worker = null;
  }
}

// ------------------------------------------------------------- worker

/** Spin up the inference worker and resolve once its three sessions are built
 *  (or reject on an init error). */
function startWorker(): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const w = new Worker(new URL('./wakeWorker.ts', import.meta.url), {
      type: 'module',
    });
    worker = w;
    let settled = false;
    w.onmessage = (e: MessageEvent) => {
      const msg = e.data as {
        type: string;
        value?: number;
        message?: string;
        fatal?: boolean;
        count?: number;
        max?: number;
      };
      switch (msg.type) {
        case 'ready':
          if (!settled) {
            settled = true;
            resolve();
          }
          break;
        case 'score':
          if (!inferredOnce) {
            inferredOnce = true;
            noteStage('inference-ok');
          }
          handleScore(msg.value ?? 0);
          break;
        case 'overflow':
          if (msg.fatal) {
            console.warn(
              '[WakeWord] inference cannot keep up with real-time audio on this ' +
                'machine; disabling wake word for this session.'
            );
            stopWakeWord();
          } else {
            console.warn(
              `[WakeWord] audio backlog overflowed (${msg.count}/${msg.max}) — dropping buffered audio.`
            );
          }
          break;
        case 'error':
          console.warn('[WakeWord] worker error (wake word inactive):', msg.message);
          if (!settled) {
            settled = true;
            reject(new Error(msg.message ?? 'worker init failed'));
          } else {
            stopWakeWord();
          }
          break;
      }
    };
    w.onerror = (e) => {
      console.warn('[WakeWord] worker crashed (wake word inactive):', e.message);
      if (!settled) {
        settled = true;
        reject(new Error(e.message || 'worker crashed'));
      } else {
        stopWakeWord();
      }
    };
    w.postMessage({
      type: 'init',
      melUrl: melspecUrl,
      embedUrl: embeddingUrl,
      wakeUrl: wakewordUrl,
      wasmSimdUrl: ortWasmSimdUrl,
      wasmUrl: ortWasmUrl,
    });
  });
}

// ------------------------------------------------------------- mic capture

/** Inline AudioWorklet processor: accumulates the native-rate mic samples and
 *  posts them to the main thread in ~2048-sample batches (keeps postMessage
 *  overhead low). Runs on the audio render thread; writes no output (silent).
 *  Loaded from a Blob URL so it resolves in dev (http://) and packaged (file://)
 *  without any asset-path plumbing. */
function ensureWorkletUrl(): string {
  if (workletUrl) return workletUrl;
  const code = `
class WakeCapture extends AudioWorkletProcessor {
  constructor() { super(); this._buf = []; }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) {
      const b = this._buf;
      for (let i = 0; i < ch.length; i++) b.push(ch[i]);
      if (b.length >= 2048) {
        this.port.postMessage(Float32Array.from(b));
        b.length = 0;
      }
    }
    return true;
  }
}
registerProcessor('wake-capture', WakeCapture);
`;
  workletUrl = URL.createObjectURL(new Blob([code], { type: 'application/javascript' }));
  return workletUrl;
}

/** Streaming linear resample of one native-rate chunk to TARGET_RATE. Keeps the
 *  fractional cursor and the previous chunk's last sample so consecutive chunks
 *  join seamlessly. */
function resampleTo16k(chunk: Float32Array): Float32Array {
  if (resampleStep === 1) return chunk; // native already 16 kHz (rare on desktop)
  const ext = new Float32Array(chunk.length + 1);
  ext[0] = resamplePrev;
  ext.set(chunk, 1);
  const last = ext.length - 1;
  const out: number[] = [];
  let t = resampleT;
  while (Math.floor(t) + 1 <= last) {
    const i = Math.floor(t);
    const f = t - i;
    out.push(ext[i] * (1 - f) + ext[i + 1] * f);
    t += resampleStep;
  }
  resampleT = t - last; // ext[last] becomes the next chunk's ext[0]
  resamplePrev = chunk[chunk.length - 1];
  return Float32Array.from(out);
}

async function openMic(): Promise<void> {
  stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true },
  });
  // Native-rate context (NOT forced 16 kHz) — the STT path's safe shape.
  audioCtx = new AudioContext();
  if (audioCtx.state === 'suspended') await audioCtx.resume();
  resampleStep = audioCtx.sampleRate / TARGET_RATE;
  resampleT = 0;
  resamplePrev = 0;
  await audioCtx.audioWorklet.addModule(ensureWorkletUrl());
  const source = audioCtx.createMediaStreamSource(stream);
  workletNode = new AudioWorkletNode(audioCtx, 'wake-capture');
  // A zero-gain sink keeps the graph pulled without routing the mic to speakers.
  sink = audioCtx.createGain();
  sink.gain.value = 0;
  workletNode.port.onmessage = (e: MessageEvent) => {
    if (!running || suspended || !worker) return;
    const buf = resampleTo16k(e.data as Float32Array);
    if (buf.length === 0) return;
    worker.postMessage({ type: 'audio', buf }, [buf.buffer]);
  };
  source.connect(workletNode);
  workletNode.connect(sink);
  sink.connect(audioCtx.destination);
}

function closeMic(): void {
  if (workletNode) {
    workletNode.port.onmessage = null;
    try { workletNode.disconnect(); } catch { /* already gone */ }
    workletNode = null;
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
 *  return to idle. Tell the worker to drop its buffers on suspend so no stale
 *  audio triggers later. */
function subscribeSuspend(): void {
  const evaluate = () => {
    const v = useVoiceStore.getState();
    const shouldSuspend = v.phase !== 'idle' || v.speaking;
    if (shouldSuspend && !suspended) {
      suspended = true;
      worker?.postMessage({ type: 'reset' });
    } else if (!shouldSuspend && suspended) {
      suspended = false;
    }
  };
  evaluate();
  unsubStore = useVoiceStore.subscribe(evaluate);
}

// ------------------------------------------------------------- trigger

function handleScore(score: number): void {
  // TEMP tuning aid: surface elevated scores so THRESHOLD can be calibrated to
  // this mic/voice. Speech near "hey jarvis" spikes; silence sits ~0.0002.
  if (score >= 0.1) {
    console.log(
      `[WakeWord] score ${score.toFixed(3)}${score >= THRESHOLD ? ' → TRIGGER' : ` (below ${THRESHOLD})`}`
    );
  }
  if (score < THRESHOLD) return;
  const now = Date.now();
  if (now - lastTriggerAt < COOLDOWN_MS) return;
  lastTriggerAt = now;
  worker?.postMessage({ type: 'reset' }); // don't re-fire on the same utterance's tail
  void useVoiceStore.getState().beginWakeListen();
}
