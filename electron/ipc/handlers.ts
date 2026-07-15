/**
 * Jarvis OS — IPC Handler Registration
 * All main-process IPC handlers are defined here.
 * Keep this file focused — one handler per capability.
 */
import { IpcMain, app, shell, BrowserWindow, Notification } from 'electron';
import { appIcon } from '../icon';

// Caps for renderer-supplied notification text: the renderer is the least
// trusted process, so anything it sends is validated and truncated here.
const NOTIFY_TITLE_MAX = 128;
const NOTIFY_BODY_MAX = 512;

/** Phase 8: the renderer arms/disarms per-session screen OCR. The actual
 *  capture loop lives in the main process (sensing.ts); the renderer only
 *  flips the intent — no image ever crosses to the renderer. */
export interface ScreenSensingControls {
  armScreenSensing: () => void;
  disarmScreenSensing: () => void;
}

export function registerIpcHandlers(
  ipcMain: IpcMain,
  summonWindow: () => Promise<void>,
  screen: ScreenSensingControls
): void {
  // ---- App version
  ipcMain.handle('get-app-version', () => {
    return app.getVersion();
  });

  // ---- Open external URL in default browser
  ipcMain.handle('open-external', async (_, url: string) => {
    if (typeof url === 'string' && (url.startsWith('http://') || url.startsWith('https://'))) {
      await shell.openExternal(url);
    }
  });

  // ---- Window controls
  ipcMain.on('window-minimize', () => {
    BrowserWindow.getFocusedWindow()?.minimize();
  });

  ipcMain.on('window-maximize', () => {
    const win = BrowserWindow.getFocusedWindow();
    if (win?.isMaximized()) {
      win.unmaximize();
    } else {
      win?.maximize();
    }
  });

  ipcMain.on('window-close', () => {
    BrowserWindow.getFocusedWindow()?.close();
  });

  // ---- Native notification (Phase 4, Part 3)
  // The renderer's push-event handler calls window.jarvis.notify(title, body);
  // the toast itself is created HERE in the main process — the renderer never
  // touches Node. Clicking the notification summons the window.
  ipcMain.on('notify', (_, payload: unknown) => {
    if (!Notification.isSupported()) return;
    const raw = (payload ?? {}) as { title?: unknown; body?: unknown };
    const title =
      typeof raw.title === 'string' ? raw.title.trim().slice(0, NOTIFY_TITLE_MAX) : '';
    const body =
      typeof raw.body === 'string' ? raw.body.trim().slice(0, NOTIFY_BODY_MAX) : '';
    if (!title && !body) return;

    const notification = new Notification({
      title: title || 'Jarvis',
      body,
      icon: appIcon(),
    });
    notification.on('click', () => void summonWindow());
    notification.show();
  });

  // ---- Screen sensing arm/disarm (Phase 8) — per-session opt-in for OCR.
  ipcMain.on('context:start-screen', () => screen.armScreenSensing());
  ipcMain.on('context:stop-screen', () => screen.disarmScreenSensing());
}
