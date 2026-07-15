/**
 * Jarvis OS — Device & Screen Sensing (Phase 8, Parts 2 + 3)
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
 * - Screen OCR capture is PER-SESSION opt-in: `screenArmed` defaults false on
 *   every launch and is only turned on by an explicit renderer action (the IPC
 *   in ipc/handlers.ts). It is additionally gated by the config's `screen_ocr`
 *   capability + the master switch. The captured frame is a DOWNSCALED
 *   thumbnail; it is POSTed and never stored on disk.
 */
import { powerMonitor, desktopCapturer } from 'electron';
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
/** Per-session opt-in for screen OCR — false on every launch, never persisted. */
let screenArmed = false;

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
 *  module. Windows-only; a spawn failure degrades to idle-only sensing. */
const WIN_HELPER_SCRIPT = `
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
// Screen OCR capture (per-session armed)
// ============================================================
async function captureAndPostScreen(): Promise<void> {
  if (!settings.enabled || !settings.screen_ocr || !screenArmed) return;
  try {
    const sources = await desktopCapturer.getSources({
      types: ['screen'],
      // Downscaled — "lightweight, no full-res images". OCR of the primary
      // display's condensed thumbnail is enough for on-screen context.
      thumbnailSize: { width: 1280, height: 800 },
    });
    const primary = sources[0];
    if (!primary) return;
    const png = primary.thumbnail.toPNG();
    if (!png || png.length === 0) return;

    const form = new FormData();
    form.append('file', new Blob([png], { type: 'image/png' }), 'frame.png');
    await fetch(backendUrl('/api/context/screen'), {
      method: 'POST',
      headers: authHeaders(), // do NOT set Content-Type — the Blob sets the boundary
      body: form,
    });
  } catch {
    // A capture/post hiccup — the next interval retries; never throw.
  }
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
  const wantScreen = settings.enabled && settings.screen_ocr && screenArmed;
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

/** Renderer opted INTO screen sensing for this session. */
export function armScreenSensing(): void {
  screenArmed = true;
  applyScreenLoop();
}

/** Renderer opted OUT (or the master switch flipped) — the kill switch. */
export function disarmScreenSensing(): void {
  screenArmed = false;
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
  stopWinHelper();
  screenArmed = false;
}
