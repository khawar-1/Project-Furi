/**
 * Jarvis OS — IPC Handler Registration
 * All main-process IPC handlers are defined here.
 * Keep this file focused — one handler per capability.
 */
import { IpcMain, app, shell, BrowserWindow } from 'electron';

export function registerIpcHandlers(ipcMain: IpcMain): void {
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
}
