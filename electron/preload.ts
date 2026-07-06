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

  // IPC: listen for backend events
  onBackendReady: (callback: () => void) => void;
  onBackendError: (callback: (error: string) => void) => void;

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

  onBackendReady: (callback: () => void) => {
    ipcRenderer.on('backend-ready', () => callback());
  },

  onBackendError: (callback: (error: string) => void) => {
    ipcRenderer.on('backend-error', (_, error: string) => callback(error));
  },

  removeAllListeners: (channel: string) => {
    ipcRenderer.removeAllListeners(channel);
  },
};

contextBridge.exposeInMainWorld('jarvis', jarvisAPI);

// Expose the backend URL as a global constant for the API client
contextBridge.exposeInMainWorld('__BACKEND_URL__', `http://localhost:${process.env.BACKEND_PORT || '8000'}`);
