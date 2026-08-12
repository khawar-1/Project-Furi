/**
 * Furi OS — Device & Screen Sensing (Phase 8, Parts 2 + 3)
 *
 * The native half of the Context Layer, running entirely in the Electron MAIN
 * process so it works while the window lives in the tray. It writes to the
 * backend over ordinary authed HTTP — never the /ws socket, which stays
 * strictly server→client.
 *
 * Privacy posture is honored here too:
 * - The MASTER kill switch lives in the backend config; main polls it and only
 *   senses while `enabled`. Flipping it off stops sensing within one poll.
 * - Device sensing (active app/window title + idle time) is lightweight — no
 *   images — via a single long-lived PowerShell/Win32 helper + Electron's
 *   built-in powerMonitor.getSystemIdleTime().
 * - Screen OCR capture runs whenever the PERSISTED opt-in allows it (master
 *   switch + `screen_ocr`, both default OFF and visible in the StatusBar
 *   indicator) — the settings toggle IS the consent, and it survives restarts.
 *   (The original per-session "arm every launch" ritual silently disarmed
 *   capture on every restart — live failure 2026-07-16: the user's opted-in
 *   screen questions went dark and the chat LLM invented a "take a
 *   screenshot" trigger phrase.) `screenPaused` is the per-session INSTANT
 *   pause (renderer IPC in ipc/handlers.ts) — a local kill that doesn't wait
 *   out the settings poll; it resets to capturing on the next launch. The
 *   captured frame is a DOWNSCALED thumbnail; POSTed, never stored on disk.
 */
import { powerMonitor, desktopCapturer, BrowserWindow } from 'electron';
import { spawn, ChildProcess } from 'child_process';

interface ContextSettings {
  enabled: boolean;
  device_sensing: boolean;
  screen_ocr: boolean;
  ocr_interval_seconds: number;
  idle_threshold_seconds: number;
}

const SETTINGS_POLL_MS = 15_000;   // how often main re-reads the backend flags
const DEVICE_HEARTBEAT_MS = 30_000; // keep presence fresh even without a change
// Screen-aware chat: a foreground-window change also triggers a capture (the
// interval loop stays as the floor). Settle lets the new window paint first;
// the min gap keeps an alt-tab burst from spamming the OCR engine (matches the
// backend's CONTEXT_MIN_OCR_INTERVAL). The backend dedupes unchanged text.
const OCR_CHANGE_SETTLE_MS = 800;
const OCR_CHANGE_MIN_GAP_MS = 5_000;
const DEFAULT_SETTINGS: ContextSettings = {
  enabled: false,
  device_sensing: true,
  screen_ocr: false,
  ocr_interval_seconds: 30,
  idle_threshold_seconds: 300,
};

let settings: ContextSettings = { ...DEFAULT_SETTINGS };
let settingsTimer: NodeJS.Timeout | null = null;
let deviceTimer: NodeJS.Timeout | null = null;
let ocrTimer: NodeJS.Timeout | null = null;
let winHelper: ChildProcess | null = null;
let lastTitle = '';
let lastApp = '';
/** Change-triggered capture state: a pending settle timer (coalesces bursts)
 *  and when the last change-triggered capture actually fired (min-gap). */
let changeCaptureTimer: NodeJS.Timeout | null = null;
let lastChangeCaptureAt = 0;
/** Per-session PAUSE for screen OCR — false (capturing) on every launch; the
 *  persisted screen_ocr setting is the consent, this is the instant local
 *  override. Never persisted. */
let screenPaused = false;

function backendUrl(path: string): string {
  const port = process.env.BACKEND_PORT || '8000';
  return `http://127.0.0.1:${port}${path}`;
}

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const token = process.env.JARVIS_AUTH_TOKEN || '';
  return token ? { 'X-Jarvis-Token': token, ...extra } : { ...extra };
}

// ============================================================
// Active-window helper (zero-dependency Win32 via PowerShell)
// ============================================================
/** A single long-lived PowerShell process that prints one JSON line whenever
 *  the foreground window changes: {"app": "<process>.exe", "title": "<title>"}.
 *  Uses user32/kernel32 P/Invoke — lightweight, no screenshots, no npm native
 *  module. Windows-only; a spawn failure degrades to idle-only sensing.
 *
 *  PARENT WATCHDOG: stopWinHelper()/before-quit is the graceful shutdown, but
 *  a hard-killed Electron (Task Manager, a Ctrl+C that never reaches
 *  before-quit) used to orphan this infinite loop forever (live zombie,
 *  2026-07-16). The helper therefore checks Electron's own PID every
 *  iteration and exits on its own the moment the parent is gone — it can
 *  never outlive the app, whatever the kill path. */
const WIN_HELPER_SCRIPT = `
$parentPid = ${process.pid}
$sig = @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public class W {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
}
'@
Add-Type $sig
$last = ""
while ($true) {
  if (-not (Get-Process -Id $parentPid -ErrorAction SilentlyContinue)) { exit }
  try {
    $h = [W]::GetForegroundWindow()
    $sb = New-Object System.Text.StringBuilder 1024
    [void][W]::GetWindowText($h, $sb, $sb.Capacity)
    $title = $sb.ToString()
    $pid2 = 0
    [void][W]::GetWindowThreadProcessId($h, [ref]$pid2)
    $app = ""
    try { $app = (Get-Process -Id $pid2 -ErrorAction Stop).ProcessName } catch {}
    $key = "$app|$title"
    if ($key -ne $last) {
      $last = $key
      $obj = @{ app = $app; title = $title } | ConvertTo-Json -Compress
      [Console]::Out.WriteLine($obj)
    }
  } catch {}
  Start-Sleep -Milliseconds 1000
}
`;

function startWinHelper(): void {
  if (winHelper || process.platform !== 'win32') return;
  try {
    winHelper = spawn(
      'powershell.exe',
      ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', WIN_HELPER_SCRIPT],
      { stdio: ['ignore', 'pipe', 'ignore'], windowsHide: true }
    );
    let buffer = '';
    winHelper.stdout?.on('data', (chunk: Buffer) => {
      buffer += chunk.toString();
      let nl: number;
      while ((nl = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (!line) continue;
        try {
          const parsed = JSON.parse(line) as { app?: string; title?: string };
          lastApp = parsed.app || '';
          lastTitle = parsed.title || '';
          void postDevice(); // a foreground change is worth posting immediately
          scheduleChangeCapture(); // …and worth a fresh screen capture too
        } catch {
          // Ignore a malformed line; the next change re-posts.
        }
      }
    });
    winHelper.on('exit', () => {
      winHelper = null; // stopped or crashed; the settings poll re-spawns if still on
    });
  } catch (err) {
    console.warn('[Sensing] Active-window helper failed to start:', err);
    winHelper = null;
  }
}

function stopWinHelper(): void {
  if (winHelper) {
    winHelper.kill();
    winHelper = null;
  }
  lastApp = '';
  lastTitle = '';
}

// ============================================================
// Device signal POST
// ============================================================
async function postDevice(): Promise<void> {
  if (!settings.enabled || !settings.device_sensing) return;
  let idle: number | null = null;
  try {
    idle = powerMonitor.getSystemIdleTime(); // seconds
  } catch {
    idle = null;
  }
  try {
    await fetch(backendUrl('/api/context/device'), {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        active_app: lastApp || null,
        window_title: lastTitle || null,
        idle_seconds: idle,
      }),
    });
  } catch {
    // Backend momentarily unreachable — the next heartbeat retries.
  }
}

// ============================================================
// Screen OCR capture (persisted opt-in; per-session pausable)
// ============================================================
async function captureAndPostScreen(): Promise<boolean> {
  if (!settings.enabled || !settings.screen_ocr || screenPaused) return false;
  // Self-observation guard (screen-aware chat): never capture while Furi
  // itself is the foreground window. Asking "what's on my screen" from inside
  // Furi means the thing the user was looking at BEFORE focusing us — and
  // OCR-ing our own chat panel would feed Furi's answers back into its own
  // context (a feedback loop). The last pre-focus capture stays "current";
  // a hidden/tray window is not focused, so wake-word asks capture normally.
  if (BrowserWindow.getFocusedWindow()) return false;
  try {
    const sources = await desktopCapturer.getSources({
      types: ['screen'],
      // Downscaled — "lightweight, no full-res images". OCR of the primary
      // display's condensed thumbnail is enough for on-screen context.
      thumbnailSize: { width: 1280, height: 800 },
    });
    const primary = sources[0];
    if (!primary) return false;
    const png = primary.thumbnail.toPNG();
    if (!png || png.length === 0) return false;

    const form = new FormData();
    form.append('file', new Blob([png], { type: 'image/png' }), 'frame.png');
    await fetch(backendUrl('/api/context/screen'), {
      method: 'POST',
      headers: authHeaders(), // do NOT set Content-Type — the Blob sets the boundary
      body: form,
    });
    return true;
  } catch {
    // A capture/post hiccup — the next interval retries; never throw.
    return false;
  }
}

/** A foreground-window change schedules one capture after a short settle
 *  delay (screen-aware chat: the on-screen context follows the user's focus
 *  instead of waiting out the interval). Bursts coalesce into the pending
 *  timer; a min gap throttles rapid alt-tabbing. captureAndPostScreen itself
 *  stays the gate (enabled + screen_ocr + not paused), so this is inert when
 *  screen sensing is off. */
function scheduleChangeCapture(): void {
  if (!settings.enabled || !settings.screen_ocr || screenPaused) return;
  if (changeCaptureTimer) return; // a capture is already pending — coalesce
  if (Date.now() - lastChangeCaptureAt < OCR_CHANGE_MIN_GAP_MS) return;
  changeCaptureTimer = setTimeout(() => {
    changeCaptureTimer = null;
    // Burn the min-gap ONLY when a capture actually happened: a fire skipped by
    // the self-observation guard (Furi was focused) must not throttle the
    // very next switch away — "focus Furi, then switch to Chrome 2s later"
    // has to capture Chrome promptly, not wait out a gap the skip started.
    void captureAndPostScreen().then((captured) => {
      if (captured) lastChangeCaptureAt = Date.now();
    });
  }, OCR_CHANGE_SETTLE_MS);
}

// ============================================================
// Loop lifecycle driven by the backend settings poll
// ============================================================
function applyDeviceLoop(): void {
  const wantDevice = settings.enabled && settings.device_sensing;
  if (wantDevice) {
    startWinHelper();
    if (!deviceTimer) {
      deviceTimer = setInterval(() => void postDevice(), DEVICE_HEARTBEAT_MS);
      void postDevice(); // an immediate first sample
    }
  } else {
    stopWinHelper();
    if (deviceTimer) {
      clearInterval(deviceTimer);
      deviceTimer = null;
    }
  }
}

function applyScreenLoop(): void {
  const wantScreen = settings.enabled && settings.screen_ocr && !screenPaused;
  if (wantScreen) {
    if (!ocrTimer) {
      const ms = Math.max(5, settings.ocr_interval_seconds) * 1000;
      ocrTimer = setInterval(() => void captureAndPostScreen(), ms);
      void captureAndPostScreen(); // an immediate first capture
    }
  } else if (ocrTimer) {
    clearInterval(ocrTimer);
    ocrTimer = null;
  }
}

async function pollSettings(): Promise<void> {
  try {
    const res = await fetch(backendUrl('/api/context/settings'), { headers: authHeaders() });
    if (res.ok) {
      const body = (await res.json()) as ContextSettings;
      const intervalChanged = body.ocr_interval_seconds !== settings.ocr_interval_seconds;
      settings = { ...DEFAULT_SETTINGS, ...body };
      applyDeviceLoop();
      // Re-arm the OCR interval if its cadence changed while active.
      if (intervalChanged && ocrTimer) {
        clearInterval(ocrTimer);
        ocrTimer = null;
      }
      applyScreenLoop();
    }
  } catch {
    // Backend not up yet / momentary error — keep the last-known settings.
  }
}

/** Renderer RESUMED screen capture after a session pause. (Exported name kept
 *  for the IPC wiring; semantics are resume — the persisted setting is the
 *  consent, capture runs by default while it allows.) */
export function armScreenSensing(): void {
  screenPaused = false;
  applyScreenLoop();
}

/** Renderer PAUSED screen capture for this session — the instant local kill
 *  switch (no settings-poll latency). Resets on the next launch. */
export function disarmScreenSensing(): void {
  screenPaused = true;
  applyScreenLoop();
}

/** Start the settings poll + sensing loops. Called once from main after the
 *  backend is healthy and the auth token is loaded. */
export function startSensing(): void {
  if (process.platform !== 'win32') {
    // The active-window helper is Windows-specific; idle-only sensing could be
    // added per-platform later. For now Phase 8's native half is Windows-only.
    console.log('[Sensing] Native device sensing is Windows-only; skipping.');
  }
  void pollSettings();
  if (!settingsTimer) {
    settingsTimer = setInterval(() => void pollSettings(), SETTINGS_POLL_MS);
  }
}

/** Stop everything (app quit). */
export function stopSensing(): void {
  if (settingsTimer) { clearInterval(settingsTimer); settingsTimer = null; }
  if (deviceTimer) { clearInterval(deviceTimer); deviceTimer = null; }
  if (ocrTimer) { clearInterval(ocrTimer); ocrTimer = null; }
  if (changeCaptureTimer) { clearTimeout(changeCaptureTimer); changeCaptureTimer = null; }
  stopWinHelper();
  screenPaused = false;
}
