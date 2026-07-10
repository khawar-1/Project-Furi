/**
 * Jarvis OS — Electron Main Process
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
import { app, BrowserWindow, Menu, Tray, globalShortcut, shell, ipcMain } from 'electron';
import { join } from 'path';
import { spawn, ChildProcess } from 'child_process';
import { registerIpcHandlers } from './ipc/handlers';
import { appIcon } from './icon';

const isDev = process.env.NODE_ENV === 'development';
const FRONTEND_DEV_URL = 'http://localhost:5173';
const BACKEND_PORT = process.env.BACKEND_PORT || '8000';
const SUMMON_HOTKEY = 'Control+Shift+J';

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let backendProcess: ChildProcess | null = null;
// Set on every real quit path (tray Quit, app.quit, OS shutdown) so the
// window's close-to-tray handler knows to let the close through.
let isQuitting = false;

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

  backendProcess = spawn(pythonBin, ['-m', 'uvicorn', 'main:app', '--port', BACKEND_PORT], {
    cwd: backendPath,
    stdio: 'pipe',
    env: { ...process.env },
  });

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
  tray.setToolTip('Jarvis OS');
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: 'Open Jarvis', click: () => void summonWindow() },
      { type: 'separator' },
      {
        label: 'Quit Jarvis',
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

function registerGlobalHotkey(): void {
  const registered = globalShortcut.register(SUMMON_HOTKEY, () => void summonWindow());
  if (!registered) {
    // Another app owns the combination — not fatal, the tray still works.
    console.warn(`[Electron] Global hotkey ${SUMMON_HOTKEY} could not be registered`);
  }
}

// ============================================================
// App Lifecycle
// ============================================================
// Single instance: launching Jarvis while it lives in the tray must summon
// the existing window, never start a second app (and second backend spawn).
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => void summonWindow());

  app.whenReady().then(async () => {
    // Windows ties toast notifications to an Application User Model ID; without
    // this, Notification.show() is silently dropped. In dev the Electron
    // binary's own identity is the one Windows has a shortcut for.
    if (process.platform === 'win32') {
      app.setAppUserModelId(isDev ? process.execPath : 'com.jarvis.os');
    }

    startBackend();
    registerIpcHandlers(ipcMain, summonWindow);
    createTray();
    registerGlobalHotkey();
    await createWindow();

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

app.on('before-quit', () => {
  isQuitting = true;
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
