/**
 * Jarvis OS — Electron Main Process
 * Creates the BrowserWindow, loads the frontend, and manages backend lifecycle.
 */
import { app, BrowserWindow, shell, ipcMain } from 'electron';
import { join } from 'path';
import { spawn, ChildProcess } from 'child_process';
import { registerIpcHandlers } from './ipc/handlers';

const isDev = process.env.NODE_ENV === 'development';
const FRONTEND_DEV_URL = 'http://localhost:5173';
const BACKEND_PORT = process.env.BACKEND_PORT || '8000';

let mainWindow: BrowserWindow | null = null;
let backendProcess: ChildProcess | null = null;

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
    },
    show: false, // Show after ready-to-show for a clean startup
    icon: join(__dirname, '../frontend/public/icon.png'),
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

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

// ============================================================
// App Lifecycle
// ============================================================
app.whenReady().then(async () => {
  startBackend();
  registerIpcHandlers(ipcMain);
  await createWindow();

  app.on('activate', async () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      await createWindow();
    }
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('before-quit', () => {
  stopBackend();
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
