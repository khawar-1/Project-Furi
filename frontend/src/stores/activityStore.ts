/**
 * Jarvis OS — Activity Store (Zustand)
 * Tool-execution audit trail for the Timeline panel (Phase 3).
 */
import { create } from 'zustand';
import type { ActivityEntry, PermissionLevel } from '@/types';
import { activityApi } from '@/lib/api';
import { useChatStore } from '@/stores/chatStore';

export type ActivityFilter = 'all' | PermissionLevel | 'failed';

interface ActivityState {
  entries: ActivityEntry[];
  isLoading: boolean;
  error: string | null;
  filter: ActivityFilter;
  sessionOnly: boolean;
  lastRefreshed: Date | null;

  /** silent = background refresh: no spinner, keep stale data on failure */
  loadActivity: (opts?: { silent?: boolean }) => Promise<void>;
  setFilter: (filter: ActivityFilter) => void;
  setSessionOnly: (sessionOnly: boolean) => void;
}

export const useActivityStore = create<ActivityState>((set, get) => ({
  entries: [],
  isLoading: false,
  error: null,
  filter: 'all',
  sessionOnly: false,
  lastRefreshed: null,

  loadActivity: async (opts) => {
    const silent = opts?.silent ?? false;
    if (!silent) set({ isLoading: true, error: null });
    try {
      const entries = get().sessionOnly
        ? await activityApi.forSession(useChatStore.getState().sessionId)
        : await activityApi.list();
      set({ entries, isLoading: false, error: null, lastRefreshed: new Date() });
    } catch (e) {
      if (silent) return; // keep showing the last good data
      set({ error: String(e), isLoading: false });
    }
  },

  setFilter: (filter) => set({ filter }),

  setSessionOnly: (sessionOnly) => {
    set({ sessionOnly });
    void get().loadActivity();
  },
}));

/** The permission/failed filter, applied client-side so switching is instant. */
export function filterEntries(entries: ActivityEntry[], filter: ActivityFilter): ActivityEntry[] {
  if (filter === 'all') return entries;
  if (filter === 'failed') return entries.filter((e) => !e.success);
  return entries.filter((e) => e.permission_level === filter);
}
