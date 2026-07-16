/**
 * Jarvis OS — Routines Store (Zustand)
 * List/run/delete state for the Routines panel (Phase 6, Part 5). Creation
 * happens through chat ("save this as a routine called X"); running from here
 * starts a background Task whose outcome arrives as a push event.
 */
import { create } from 'zustand';
import type { Routine, RoutineSchedule } from '@/types';
import { routinesApi } from '@/lib/api';

interface RoutinesState {
  routines: Routine[];
  isLoading: boolean;
  error: string | null;
  deletingId: string | null;
  runningId: string | null;
  savingScheduleId: string | null;

  /** silent = background refresh: no spinner, keep stale data on failure */
  loadRoutines: (opts?: { silent?: boolean }) => Promise<void>;
  deleteRoutine: (id: string) => Promise<void>;
  runRoutine: (id: string) => Promise<void>;
  setSchedule: (id: string, schedule: Partial<RoutineSchedule>) => Promise<void>;
}

export const useRoutinesStore = create<RoutinesState>((set) => ({
  routines: [],
  isLoading: false,
  error: null,
  deletingId: null,
  runningId: null,
  savingScheduleId: null,

  loadRoutines: async (opts) => {
    const silent = opts?.silent ?? false;
    if (!silent) set({ isLoading: true, error: null });
    try {
      const routines = await routinesApi.list();
      set({ routines, isLoading: false, error: null });
    } catch (e) {
      if (silent) return; // keep showing the last good data
      set({ error: String(e), isLoading: false });
    }
  },

  deleteRoutine: async (id: string) => {
    set({ deletingId: id });
    try {
      await routinesApi.delete(id);
      set((state) => ({
        routines: state.routines.filter((r) => r.id !== id),
        deletingId: null,
      }));
    } catch (e) {
      set({ error: String(e), deletingId: null });
    }
  },

  runRoutine: async (id: string) => {
    set({ runningId: id, error: null });
    try {
      await routinesApi.run(id);
    } catch (e) {
      set({ error: String(e) });
    } finally {
      set({ runningId: null });
    }
  },

  setSchedule: async (id: string, schedule: Partial<RoutineSchedule>) => {
    set({ savingScheduleId: id, error: null });
    try {
      const updated = await routinesApi.setSchedule(id, schedule);
      set((state) => ({
        routines: state.routines.map((r) => (r.id === id ? updated : r)),
        savingScheduleId: null,
      }));
    } catch (e) {
      set({ error: String(e), savingScheduleId: null });
    }
  },
}));
