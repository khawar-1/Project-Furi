/**
 * Jarvis OS — Push Channel State (Zustand, Phase 4)
 * Connection status of the server→client WebSocket. The connection itself
 * lives in lib/push.ts; this store only mirrors its state for the UI.
 */
import { create } from 'zustand';

interface PushState {
  /** True while the /ws socket is open. */
  connected: boolean;
  /** ISO timestamp of the last event received (any type). */
  lastEventAt: string | null;

  setConnected: (connected: boolean) => void;
  markEvent: (ts: string) => void;
}

export const usePushStore = create<PushState>((set) => ({
  connected: false,
  lastEventAt: null,

  setConnected: (connected) => set({ connected }),
  markEvent: (ts) => set({ lastEventAt: ts }),
}));
