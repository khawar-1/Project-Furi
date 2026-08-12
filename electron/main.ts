/**
 * Furi OS — Electron Main Process
 * Creates the BrowserWindow, loads the frontend, and manages backend lifecycle.
 *
 * Phase 4, Part 3 — ambient presence:
 * - Closing the window hides it to the system tray instead of quitting; the
 *   app (and its push channel) keeps running. Only the tray's Quit — or a
 *   real app quit — actually exits.
 * - A global hotkey (Ctrl+Shift+J) summons/focuses the window from anywhere.
 * - Native notifications are shown by the main process on behalf of the
 *   renderer (see ipc/handlers.ts); clicking one summons the window.
 */
import { app, BrowserWindow, Menu, Tray, crashReporter, globalShortcut, session, shell, ipcMain } from 'electron';
import { join } from 'path';
import { homedir } from 'os';
import { appendFile, mkdir, readFile } from 'fs/promises';
import { spawn, ChildProcess } from 'child_process';
import { registerIpcHandlers } from './ipc/handlers';
import { appIcon } from './icon';
import {
  startSensing,
  stopSensing,
  armScreenSensing,
  disarmScreenSensing,
} from './sensing';

const isDev = process.env.NODE_ENV === 'development';
const FRONTEND_DEV_URL = 'http://localhost:5173';
const BACKEND_PORT = process.env.BACKEND_PORT || '8000';
// Loopback unless deliberately overridden — the main API runs shell commands.
const BACKEND_HOST = process.env.BACKEND_HOST || '127.0.0.1';
const SUMMON_HOTKEY = 'Control+Shift+J';

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let backendProcess: ChildProcess | null = null;
// Set on every real quit path (tray Quit, app.quit, OS shutdown) so the
// window's close-to-tray handler knows to let the close through.
let isQuitting = false;
// Renderer crash-loop guard (see the render-process-gone handler).
let rendererCrashCount = 0;
let lastRendererCrashAt = 0;

// ============================================================
// Crash Forensics
// ============================================================
// Terminal lines scroll away and the user shouldn't have to fish for them —
// on a desktop app the crash record must be DURABLE (live failure 2026-07-16:
// the renderer died repeatedly right after wake-word start and the reason was
// only ever printed to the dev terminal). Every process-gone event is also
// appended to ~/.jarvis/logs/renderer-crashes.log (the backend's ~/.jarvis
// home), best-effort — a logging failure never affects the app.
const CRASH_LOG_DIR = join(homedir(), '.jarvis', 'logs');
const CRASH_LOG_PATH = join(CRASH_LOG_DIR, 'renderer-crashes.log');
function logCrashToFile(line: string): void {
  void mkdir(CRASH_LOG_DIR, { recursive: true })
    .then(() => appendFile(CRASH_LOG_PATH, `${new Date().toISOString()} ${line}\n`))
    .catch(() => undefined);
}
// Local-only Crashpad: minidumps land in app.getPath('crashDumps') and are
// NEVER uploaded anywhere — they exist so a native crash ('crashed' rather
// than 'oom') leaves a stack we can inspect on this machine.
crashReporter.start({ uploadToServer: false });

// ============================================================
// Backend Process Management
// ============================================================
function startBackend(): void {
  if (isDev) {
    // In dev, backend is started separately via `npm run backend`
    console.log('[Electron] Dev mode: backend should be running on port', BACKEND_PORT);
    return;
  }

  // In production, spawn the bundled backend
  const backendPath = join(app.getAppPath(), 'backend');
  const pythonBin = process.platform === 'win32' ? 'python' : 'python3';

  // ⚠️ --host IS EXPLICIT. Until 2026-08-03 it was omitted here and in the dev
  // script, so loopback held only by uvicorn's DEFAULT — `BACKEND_HOST` in .env
  // was decorative and setting it changed nothing in either direction. The main
  // API can delete files and run shell commands; which interface it listens on
  // must be stated, not inherited.
  backendProcess = spawn(
    pythonBin,
    ['-m', 'uvicorn', 'main:app', '--host', BACKEND_HOST, '--port', BACKEND_PORT],
    {
      cwd: backendPath,
      stdio: 'pipe',
      env: { ...process.env },
    }
  );

  backendProcess.stdout?.on('data', (data: Buffer) => {
    console.log('[Backend]', data.toString().trim());
  });

  backendProcess.stderr?.on('data', (data: Buffer) => {
    console.error('[Backend Error]', data.toString().trim());
  });

  backendProcess.on('exit', (code) => {
    console.log(`[Backend] Process exited with code ${code}`);
  });

  console.log('[Electron] Backend process started');
}

function stopBackend(): void {
  if (backendProcess) {
    backendProcess.kill('SIGTERM');
    backendProcess = null;
    console.log('[Electron] Backend process stopped');
  }
}

/** Wait for the spawned backend to start serving before we open the window, so
 *  the renderer's first /health, /api/settings/voice and /ws requests don't
 *  hit a not-yet-bound port (the ERR_CONNECTION_REFUSED startup race). Polls
 *  /health with the global fetch (no new dependency) and resolves the moment
 *  it gets a 200. On timeout it resolves anyway — the window still opens, and
 *  the frontend's health re-poll + WS auto-reconnect cover a slow/failed
 *  backend. Only used in production; in dev the `wait-on` gate in the `dev`
 *  npm script already ensured the backend was up before Electron launched. */
async function waitForBackend(timeoutMs = 30_000, intervalMs = 200): Promise<void> {
  const url = `http://127.0.0.1:${BACKEND_PORT}/health`;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url);
      if (res.ok) return;
    } catch {
      // Not up yet — keep polling until the deadline.
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  console.warn('[Electron] Backend did not become healthy before timeout; opening window anyway');
}

/** The backend generates/persists a static API auth token at
 *  ~/.jarvis/auth_token during startup (app/core/auth.py); every renderer
 *  request must carry it. Read it here — after waitForBackend() in prod, so
 *  it exists — and stash it in process.env BEFORE createWindow(): the
 *  renderer child process inherits the env, and preload exposes it as
 *  window.__JARVIS_TOKEN__ (the proven BACKEND_PORT mechanism). Retries
 *  briefly for the dev race where Electron launches while the separately-run
 *  backend is still booting for the very first time. Failure is non-fatal:
 *  requests will 401 visibly rather than the app failing to open. */
async function loadAuthToken(retries = 10, intervalMs = 500): Promise<void> {
  const tokenPath = join(homedir(), '.jarvis', 'auth_token');
  for (let attempt = 0; attempt < retries; attempt++) {
    try {
      const token = (await readFile(tokenPath, 'utf-8')).trim();
      if (token) {
        process.env.JARVIS_AUTH_TOKEN = token;
        console.log('[Electron] API auth token loaded');
        return;
      }
    } catch {
      // Not written yet — keep retrying until the deadline.
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  console.warn(`[Electron] No auth token at ${tokenPath} — API requests will be rejected (401)`);
}

// ============================================================
// Window Creation
// ============================================================
async function createWindow(): Promise<void> {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: '#0A0A0F',
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    frame: process.platform !== 'darwin',
    webPreferences: {
      preload: join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      webSecurity: !isDev, // Allow localhost in dev
      // The window spends its life hidden in the tray; the renderer owns the
      // push-channel WebSocket and its reconnect timers, so it must never be
      // throttled while hidden or notifications would arrive late.
      backgroundThrottling: false,
    },
    show: false, // Show after ready-to-show for a clean startup
    icon: appIcon(),
  });

  // Smooth show after content loads
  mainWindow.once('ready-to-show', () => {
    mainWindow?.show();
    if (isDev) {
      mainWindow?.webContents.openDevTools({ mode: 'detach' });
    }
  });

  // Load the app
  if (isDev) {
    await mainWindow.loadURL(FRONTEND_DEV_URL);
  } else {
    await mainWindow.loadFile(join(__dirname, '../frontend/dist/index.html'));
  }

  // Open external links in the default browser, not Electron
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // Close-to-tray: hide instead of closing so the app stays ambient. The
  // renderer (and its /ws push connection) keeps running in the hidden window.
  mainWindow.on('close', (event) => {
    if (!isQuitting) {
      event.preventDefault();
      mainWindow?.hide();
    }
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // A dead renderer must never leave a permanently blank window (live failure
  // 2026-07-16: the renderer died shortly after wake-word start — DevTools
  // disconnected, the window stayed black, and NOTHING was logged, so the
  // crash reason was unknowable). Log the real reason (Chromium reports 'oom'
  // distinctly from 'crashed') and reload the page, capped so a crash loop
  // can't spin forever.
  mainWindow.webContents.on('render-process-gone', (_event, details) => {
    console.error(
      `[Electron] Renderer process gone: reason=${details.reason} exitCode=${details.exitCode}`
    );
    logCrashToFile(`renderer-gone reason=${details.reason} exitCode=${details.exitCode}`);
    if (details.reason === 'clean-exit') return;
    const now = Date.now();
    if (now - lastRendererCrashAt > 60_000) rendererCrashCount = 0;
    lastRendererCrashAt = now;
    rendererCrashCount += 1;
    if (rendererCrashCount > 3) {
      console.error('[Electron] Renderer crashed repeatedly — leaving it down; check the reason above.');
      return;
    }
    console.error(`[Electron] Reloading the window (attempt ${rendererCrashCount}/3).`);
    mainWindow?.webContents.reload();
  });
}

/** Bring the window to the user: recreate it if destroyed, restore it if
 *  minimized, show it if hidden in the tray, and focus it. Used by the tray,
 *  the global hotkey, notification clicks, and second app instances. */
async function summonWindow(): Promise<void> {
  if (mainWindow === null) {
    await createWindow();
    return;
  }
  if (mainWindow.isMinimized()) {
    mainWindow.restore();
  }
  mainWindow.show();
  mainWindow.focus();
}

// ============================================================
// Tray & Global Hotkey (Phase 4, Part 3)
// ============================================================
function createTray(): void {
  tray = new Tray(appIcon());
  tray.setToolTip('Furi OS');
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: 'Open Furi', click: () => void summonWindow() },
      { type: 'separator' },
      {
        label: 'Quit Furi',
        click: () => {
          isQuitting = true;
          app.quit();
        },
      },
    ])
  );
  // Windows convention: left-click opens the app, right-click opens the menu
  // (the context menu is bound automatically).
  tray.on('click', () => void summonWindow());
}

// ============================================================
// Microphone Permission (Phase 7, Part 2 — push-to-talk)
// ============================================================
/** Only our own renderer may use the microphone: the Vite dev server in dev,
 *  the bundled file:// page in production. Everything else — and every other
 *  permission type — is denied. No preload bridge is involved: getUserMedia
 *  works directly in the sandboxed renderer once the permission is granted,
 *  and the audio bytes only ever travel to the loopback backend. */
function isOwnRenderer(requestingUrl: string): boolean {
  return isDev
    ? requestingUrl.startsWith(FRONTEND_DEV_URL)
    : requestingUrl.startsWith('file://');
}

function registerMediaPermissionHandlers(): void {
  session.defaultSession.setPermissionRequestHandler(
    (_webContents, permission, callback, details) => {
      callback(permission === 'media' && isOwnRenderer(details.requestingUrl));
    }
  );
  // The synchronous twin: navigator.permissions.query and some getUserMedia
  // paths consult this instead of raising a request.
  session.defaultSession.setPermissionCheckHandler(
    (_webContents, permission, requestingOrigin) => {
      return permission === 'media' && isOwnRenderer(requestingOrigin);
    }
  );
}

/** Phase 7, Part 5 — the "Furi moment": ONLY the hotkey path announces
 *  itself to the renderer ('summoned-by-hotkey'), so opt-in hands-free
 *  listening starts exactly when the user pressed Ctrl+Shift+J — never on a
 *  tray click, a toast click, or a second app launch. */
function notifySummonedByHotkey(): void {
  const contents = mainWindow?.webContents;
  if (!contents || contents.isDestroyed()) return;
  if (contents.isLoading()) {
    // The window was just recreated from the tray-destroyed state — give the
    // renderer a beat after load so its listener exists. Best-effort: a
    // summon racing a cold window may drop the listen-start; the window is
    // open either way and the mic is one tap away.
    contents.once('did-finish-load', () => {
      setTimeout(() => {
        if (!contents.isDestroyed()) contents.send('summoned-by-hotkey');
      }, 500);
    });
    return;
  }
  contents.send('summoned-by-hotkey');
}

function registerGlobalHotkey(): void {
  const registered = globalShortcut.register(SUMMON_HOTKEY, () => {
    void summonWindow().then(() => notifySummonedByHotkey());
  });
  if (!registered) {
    // Another app owns the combination — not fatal, the tray still works.
    console.warn(`[Electron] Global hotkey ${SUMMON_HOTKEY} could not be registered`);
  }
}

// ============================================================
// App Lifecycle
// ============================================================
// Single instance: launching Furi while it lives in the tray must summon
// the existing window, never start a second app (and second backend spawn).
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => void summonWindow());

  app.whenReady().then(async () => {
    // Windows ties toast notifications to an Application User Model ID; without
    // this, Notification.show() is silently dropped. In dev the Electron
    // binary's own identity is the one Windows has a shortcut for.
    //
    // ⚠️ 'com.jarvis.os' KEPT THROUGH THE FURI RENAME, DELIBERATELY. It must
    // stay byte-identical to package.json's build.appId, and changing that pair
    // changes the app's INSTALL identity on Windows — an existing install
    // becomes a different application. The rename was cosmetic by decision;
    // this is one of the identifiers it deliberately did not touch.
    if (process.platform === 'win32') {
      app.setAppUserModelId(isDev ? process.execPath : 'com.jarvis.os');
    }

    startBackend();
    registerIpcHandlers(ipcMain, summonWindow, {
      armScreenSensing,
      disarmScreenSensing,
    });
    registerMediaPermissionHandlers();
    createTray();
    registerGlobalHotkey();
    // Production: don't open the window until the spawned backend is serving,
    // or its first requests hit a dead port (the startup connection-refused
    // race). In dev the `wait-on` gate already handled this before launch.
    if (!isDev) await waitForBackend();
    // The token file exists once the backend is healthy; must land in
    // process.env before the window (and its preload) is created.
    await loadAuthToken();
    await createWindow();

    // Phase 8: start the Context Layer's native sensing loop. It polls the
    // backend for the master switch and senses only while enabled; screen OCR
    // stays disarmed until the renderer opts in (per-session). Needs the auth
    // token (loaded above) to post.
    startSensing();

    app.on('activate', async () => {
      if (BrowserWindow.getAllWindows().length === 0) {
        await createWindow();
      }
    });
  });
}

// Close-to-tray means the app deliberately outlives its windows on every
// platform — only the tray Quit (or an OS-level quit) ends it.
app.on('window-all-closed', () => {
  // Intentionally empty: the tray keeps the app alive; summonWindow()
  // recreates the window on demand.
});

// GPU/utility process deaths blank the window without touching the renderer —
// log them too so a black screen is always attributable from the terminal.
app.on('child-process-gone', (_event, details) => {
  if (details.reason !== 'clean-exit' && details.reason !== 'killed') {
    console.error(
      `[Electron] Child process gone: type=${details.type} reason=${details.reason} exitCode=${details.exitCode}`
    );
    logCrashToFile(
      `child-gone type=${details.type} reason=${details.reason} exitCode=${details.exitCode}`
    );
  }
});

app.on('before-quit', () => {
  isQuitting = true;
  stopSensing();
  stopBackend();
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
});

// Security: Prevent navigation to unexpected URLs
app.on('web-contents-created', (_, contents) => {
  contents.on('will-navigate', (event, url) => {
    const allowedOrigins = [
      'http://localhost:5173',
      'http://localhost:8000',
      'file://',
    ];
    const isAllowed = allowedOrigins.some((origin) => url.startsWith(origin));
    if (!isAllowed) {
      event.preventDefault();
      shell.openExternal(url);
    }
  });
});
