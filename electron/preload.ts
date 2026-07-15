/**
 * Jarvis OS — Electron Preload Script
 * Exposes a typed `window.jarvis` API to the renderer via contextBridge.
 * This is the only bridge between the sandboxed renderer and Node.js.
 */
import { contextBridge, ipcRenderer } from 'electron';

// ============================================================
// Type Definitions (mirrored in frontend/src/types/index.ts)
// ============================================================
export interface JarvisAPI {
  // Backend communication
  getBackendUrl: () => string;

  // IPC: open external links
  openExternal: (url: string) => void;

  // IPC: window controls
  minimizeWindow: () => void;
  maximizeWindow: () => void;
  closeWindow: () => void;

  // IPC: system info
  getPlatform: () => string;
  getAppVersion: () => Promise<string>;

  // IPC: native notification (Phase 4, Part 3) — the main process shows the
  // toast; clicking it summons the window. Plain strings only.
  notify: (title: string, body: string) => void;

  // IPC: listen for backend events
  onBackendReady: (callback: () => void) => void;
  onBackendError: (callback: (error: string) => void) => void;

  // IPC: the global hotkey summoned the window (Phase 7, Part 5) — the ONE
  // new bridge method of this part. The renderer may start an opt-in
  // hands-free recording in response; no data crosses the bridge.
  onSummoned: (callback: () => void) => void;

  // IPC: arm/disarm per-session screen OCR sensing (Phase 8). The renderer
  // only flips the intent; the capture loop runs in the main process and no
  // image ever crosses the bridge.
  startScreenSensing: () => void;
  stopScreenSensing: () => void;

  // Cleanup
  removeAllListeners: (channel: string) => void;
}

// ============================================================
// API Exposure
// ============================================================
const jarvisAPI: JarvisAPI = {
  getBackendUrl: () => {
    const port = process.env.BACKEND_PORT || '8000';
    return `http://localhost:${port}`;
  },

  openExternal: (url: string) => {
    ipcRenderer.invoke('open-external', url);
  },

  minimizeWindow: () => ipcRenderer.send('window-minimize'),
  maximizeWindow: () => ipcRenderer.send('window-maximize'),
  closeWindow: () => ipcRenderer.send('window-close'),

  getPlatform: () => process.platform,

  getAppVersion: () => ipcRenderer.invoke('get-app-version'),

  notify: (title: string, body: string) => {
    ipcRenderer.send('notify', { title, body });
  },

  onBackendReady: (callback: () => void) => {
    ipcRenderer.on('backend-ready', () => callback());
  },

  onBackendError: (callback: (error: string) => void) => {
    ipcRenderer.on('backend-error', (_, error: string) => callback(error));
  },

  onSummoned: (callback: () => void) => {
    ipcRenderer.on('summoned-by-hotkey', () => callback());
  },

  startScreenSensing: () => ipcRenderer.send('context:start-screen'),
  stopScreenSensing: () => ipcRenderer.send('context:stop-screen'),

  removeAllListeners: (channel: string) => {
    ipcRenderer.removeAllListeners(channel);
  },
};

contextBridge.exposeInMainWorld('jarvis', jarvisAPI);

// Expose the backend URL as a global constant for the API client
contextBridge.exposeInMainWorld('__BACKEND_URL__', `http://localhost:${process.env.BACKEND_PORT || '8000'}`);

// The API auth token, read from ~/.jarvis/auth_token by the main process
// (loadAuthToken) before this window was created and inherited via env.
// Every backend call must carry it (X-Jarvis-Token / ?token= on the WS).
contextBridge.exposeInMainWorld('__JARVIS_TOKEN__', process.env.JARVIS_AUTH_TOKEN || '');
