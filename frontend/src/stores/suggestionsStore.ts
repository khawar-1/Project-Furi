/**
 * Furi OS — Suggestions Store (Zustand) — Phase 9, the Initiative Engine
 *
 * The proactive suggestion feed: list + accept/dismiss, plus a live receiver
 * for pushed `suggestion` events. Mirrors remindersStore (silent poll, keep
 * stale data on a background failure) with an optimistic accept/dismiss that
 * reverts on error. Accepting a suggestion that carries a goal starts an
 * approval-gated background Task server-side — its PlanCard/outcome arrives
 * through the existing `task` push channel, not through this store.
 */
import { create } from 'zustand';
import type { Suggestion } from '@/types';
import { initiativeApi } from '@/lib/api';

/** Coerce a raw `suggestion` push payload into a feed row for instant display.
 *  The push carries `suggestion_id` (not `id`) + the display fields; the 15s
 *  poll reconciles the authoritative row (status/expiry) shortly after. */
function fromPush(payload: Record<string, unknown>): Suggestion | null {
  const id = typeof payload.suggestion_id === 'string' ? payload.suggestion_id : null;
  const title = typeof payload.title === 'string' ? payload.title : null;
  const body = typeof payload.body === 'string' ? payload.body : null;
  if (!id || !title || !body) return null;
  const str = (v: unknown, fallback: string) => (typeof v === 'string' ? v : fallback);
  const now = new Date().toISOString();
  return {
    id,
    session_id: typeof payload.session_id === 'string' ? payload.session_id : null,
    category: str(payload.category, 'general'),
    title,
    body,
    rationale: str(payload.rationale, ''),
    autonomy: str(payload.autonomy, 'suggest') as Suggestion['autonomy'],
    priority: str(payload.priority, 'normal') as Suggestion['priority'],
    goal: null,
    status: 'pending',
    task_id: null,
    created_at: now,
    updated_at: now,
    expires_at: null,
  };
}

interface SuggestionsState {
  suggestions: Suggestion[];
  isLoading: boolean;
  error: string | null;
  busyId: string | null;

  /** silent = background refresh: no spinner, keep stale data on failure */
  loadSuggestions: (opts?: { silent?: boolean }) => Promise<void>;
  accept: (id: string) => Promise<void>;
  dismiss: (id: string) => Promise<void>;
  /** A live pushed suggestion — prepend it so an open feed updates instantly. */
  receiveSuggestion: (payload: Record<string, unknown>) => void;
}

export const useSuggestionsStore = create<SuggestionsState>((set, get) => ({
  suggestions: [],
  isLoading: false,
  error: null,
  busyId: null,

  loadSuggestions: async (opts) => {
    const silent = opts?.silent ?? false;
    if (!silent) set({ isLoading: true, error: null });
    try {
      const suggestions = await initiativeApi.listSuggestions();
      set({ suggestions, isLoading: false, error: null });
    } catch (e) {
      if (silent) return; // keep showing the last good data
      set({ error: String(e), isLoading: false });
    }
  },

  accept: async (id: string) => {
    const before = get().suggestions;
    set({
      busyId: id,
      suggestions: before.map((s) => (s.id === id ? { ...s, status: 'accepted' } : s)),
    });
    try {
      const updated = await initiativeApi.accept(id);
      set((state) => ({
        suggestions: state.suggestions.map((s) => (s.id === id ? updated : s)),
        busyId: null,
      }));
    } catch (e) {
      set({ suggestions: before, error: String(e), busyId: null }); // revert
    }
  },

  dismiss: async (id: string) => {
    const before = get().suggestions;
    set({
      busyId: id,
      suggestions: before.map((s) => (s.id === id ? { ...s, status: 'dismissed' } : s)),
    });
    try {
      const updated = await initiativeApi.dismiss(id);
      set((state) => ({
        suggestions: state.suggestions.map((s) => (s.id === id ? updated : s)),
        busyId: null,
      }));
    } catch (e) {
      set({ suggestions: before, error: String(e), busyId: null }); // revert
    }
  },

  receiveSuggestion: (payload) => {
    const row = fromPush(payload);
    if (!row) return;
    set((state) => {
      if (state.suggestions.some((s) => s.id === row.id)) return state;
      return { suggestions: [row, ...state.suggestions] };
    });
  },
}));
