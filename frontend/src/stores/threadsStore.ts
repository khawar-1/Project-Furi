/**
 * Furi OS — Goal Threads Store (Zustand)
 * List/resolve/dismiss state for the Threads panel (Phase 11.3). Threads are
 * mostly captured from conversation by the extractor; the Initiative Engine
 * nudges the open ones. Resolving one ("it landed") stops the nudges.
 */
import { create } from 'zustand';
import type { GoalThread } from '@/types';
import { threadsApi } from '@/lib/api';

interface ThreadsState {
  threads: GoalThread[];
  isLoading: boolean;
  error: string | null;
  busyId: string | null;

  loadThreads: (opts?: { silent?: boolean }) => Promise<void>;
  resolveThread: (id: string) => Promise<void>;
  dismissThread: (id: string) => Promise<void>;
}

export const useThreadsStore = create<ThreadsState>((set) => ({
  threads: [],
  isLoading: false,
  error: null,
  busyId: null,

  loadThreads: async (opts) => {
    const silent = opts?.silent ?? false;
    if (!silent) set({ isLoading: true, error: null });
    try {
      const threads = await threadsApi.list();
      set({ threads, isLoading: false, error: null });
    } catch (e) {
      if (silent) return;
      set({ error: String(e), isLoading: false });
    }
  },

  resolveThread: async (id: string) => {
    set({ busyId: id, error: null });
    try {
      const updated = await threadsApi.resolve(id);
      set((s) => ({ threads: s.threads.map((t) => (t.id === id ? updated : t)), busyId: null }));
    } catch (e) {
      set({ error: String(e), busyId: null });
    }
  },

  dismissThread: async (id: string) => {
    set({ busyId: id, error: null });
    try {
      const updated = await threadsApi.dismiss(id);
      set((s) => ({ threads: s.threads.map((t) => (t.id === id ? updated : t)), busyId: null }));
    } catch (e) {
      set({ error: String(e), busyId: null });
    }
  },
}));
