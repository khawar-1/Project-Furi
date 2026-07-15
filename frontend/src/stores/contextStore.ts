/**
 * Jarvis OS — Context Layer State (Zustand, Phase 8)
 *
 * The sensing config + live status + world-model audit. Settings changes PUT
 * IMMEDIATELY (the FileIndexCard/DailyBriefingCard lesson — optimistic, revert
 * on error) so a toggle is never a no-op that waits for a separate save.
 *
 * Screen OCR has a per-session runtime "armed" state that is NOT persisted:
 * arming/disarming goes to the Electron main process (which owns the capture
 * loop) via the preload bridge. `screenArmed` mirrors that intent for the UI.
 */
import { create } from 'zustand';
import { contextApi } from '@/lib/api';
import type { ContextSettings, ContextStatus, WorldModel } from '@/types';

interface ContextState {
  settings: ContextSettings | null;
  status: ContextStatus | null;
  world: WorldModel | null;
  /** Per-session screen-OCR capture intent (renderer→main; not persisted). */
  screenArmed: boolean;
  error: string | null;

  fetchSettings: () => Promise<void>;
  updateSettings: (patch: Partial<ContextSettings>) => Promise<void>;
  fetchStatus: () => Promise<void>;
  fetchWorld: () => Promise<void>;
  armScreen: () => void;
  disarmScreen: () => void;
}

export const useContextStore = create<ContextState>((set, get) => ({
  settings: null,
  status: null,
  world: null,
  screenArmed: false,
  error: null,

  fetchSettings: async () => {
    try {
      const settings = await contextApi.getSettings();
      set({ settings, error: null });
    } catch {
      // Leave the last-known settings; a transient failure is not fatal.
    }
  },

  updateSettings: async (patch) => {
    const current = get().settings;
    if (!current) return;
    const next = { ...current, ...patch };
    set({ settings: next, error: null }); // optimistic
    try {
      const saved = await contextApi.updateSettings(next);
      set({ settings: saved });
      // Turning the master switch (or the OCR capability) off must also stop a
      // live screen-capture session.
      if ((!saved.enabled || !saved.screen_ocr) && get().screenArmed) {
        get().disarmScreen();
      }
    } catch (e) {
      set({ settings: current, error: (e as Error).message }); // revert
    }
  },

  fetchStatus: async () => {
    try {
      set({ status: await contextApi.getStatus() });
    } catch {
      // Ignore — the indicator just keeps its last state.
    }
  },

  fetchWorld: async () => {
    try {
      set({ world: await contextApi.getWorld() });
    } catch {
      // Ignore — the audit panel keeps its last snapshot.
    }
  },

  armScreen: () => {
    window.jarvis?.startScreenSensing?.();
    set({ screenArmed: true });
  },

  disarmScreen: () => {
    window.jarvis?.stopScreenSensing?.();
    set({ screenArmed: false });
  },
}));
