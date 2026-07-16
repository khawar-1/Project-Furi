/**
 * Jarvis OS — Context Layer State (Zustand, Phase 8)
 *
 * The sensing config + live status + world-model audit. Settings changes PUT
 * IMMEDIATELY (the FileIndexCard/DailyBriefingCard lesson — optimistic, revert
 * on error) so a toggle is never a no-op that waits for a separate save.
 *
 * Screen capture runs whenever the PERSISTED opt-in allows it (master +
 * screen_ocr) — the settings toggle is the consent and survives restarts.
 * `screenPaused` mirrors the per-session INSTANT pause owned by the Electron
 * main process (preload bridge): a local kill switch with no settings-poll
 * latency, reset to capturing on every launch. (The old per-session "arm"
 * silently disarmed capture on every restart — live failure 2026-07-16.)
 */
import { create } from 'zustand';
import { contextApi } from '@/lib/api';
import type { ContextSettings, ContextStatus, WorldModel } from '@/types';

interface ContextState {
  settings: ContextSettings | null;
  status: ContextStatus | null;
  world: WorldModel | null;
  /** Per-session screen-capture pause (renderer→main; not persisted). */
  screenPaused: boolean;
  error: string | null;

  fetchSettings: () => Promise<void>;
  updateSettings: (patch: Partial<ContextSettings>) => Promise<void>;
  fetchStatus: () => Promise<void>;
  fetchWorld: () => Promise<void>;
  pauseScreen: () => void;
  resumeScreen: () => void;
}

export const useContextStore = create<ContextState>((set, get) => ({
  settings: null,
  status: null,
  world: null,
  screenPaused: false,
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

  pauseScreen: () => {
    window.jarvis?.stopScreenSensing?.();
    set({ screenPaused: true });
  },

  resumeScreen: () => {
    window.jarvis?.startScreenSensing?.();
    set({ screenPaused: false });
  },
}));
