/**
 * Jarvis OS — Reminders Store (Zustand)
 * List/cancel state for the Reminders panel (Phase 4, Part 4).
 */
import { create } from 'zustand';
import type { Reminder } from '@/types';
import { remindersApi } from '@/lib/api';

interface RemindersState {
  reminders: Reminder[];
  isLoading: boolean;
  error: string | null;
  cancellingId: string | null;

  /** silent = background refresh: no spinner, keep stale data on failure */
  loadReminders: (opts?: { silent?: boolean }) => Promise<void>;
  cancelReminder: (id: string) => Promise<void>;
}

export const useRemindersStore = create<RemindersState>((set, get) => ({
  reminders: [],
  isLoading: false,
  error: null,
  cancellingId: null,

  loadReminders: async (opts) => {
    const silent = opts?.silent ?? false;
    if (!silent) set({ isLoading: true, error: null });
    try {
      const reminders = await remindersApi.list();
      set({ reminders, isLoading: false, error: null });
    } catch (e) {
      if (silent) return; // keep showing the last good data
      set({ error: String(e), isLoading: false });
    }
  },

  cancelReminder: async (id: string) => {
    set({ cancellingId: id });
    try {
      await remindersApi.cancel(id);
      set((state) => ({
        reminders: state.reminders.map((r) =>
          r.id === id ? { ...r, status: 'cancelled' } : r
        ),
        cancellingId: null,
      }));
    } catch (e) {
      set({ error: String(e), cancellingId: null });
    }
  },
}));
