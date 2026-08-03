/**
 * Jarvis OS — About Me Store (Zustand)
 * State for the "About Me" panel: user-specific facts only.
 * Fetches SemanticMemory where subject IN ('user', 'shared').
 */
import { create } from 'zustand';
import type { SemanticMemory } from '@/types';
import { memoryApi } from '@/lib/api';

interface AboutMeState {
  // State
  memories: SemanticMemory[];
  /**
   * Facts the housekeeping pass set aside — long unused, hidden from retrieval,
   * NEVER deleted. Kept as its own list rather than mixed into `memories`,
   * because that separation is what "archived" means. This is the trust
   * surface: an automatic tidy-up nobody can inspect is indistinguishable from
   * data loss.
   */
  archived: SemanticMemory[];
  isLoading: boolean;
  searchQuery: string;
  error: string | null;

  // Actions
  loadFacts: () => Promise<void>;
  loadArchived: () => Promise<void>;
  restoreFact: (id: string) => Promise<void>;
  searchFacts: (q: string) => Promise<void>;
  addFact: (content: string, category?: string) => Promise<void>;
  deleteFact: (id: string) => Promise<void>;
  setSearchQuery: (q: string) => void;
}

export const useMemoryStore = create<AboutMeState>((set, get) => ({
  memories: [],
  archived: [],
  isLoading: false,
  searchQuery: '',
  error: null,

  loadFacts: async () => {
    set({ isLoading: true, error: null });
    try {
      // No subject param = backend defaults to user + shared facts
      const result = await memoryApi.list({ limit: 200 });
      set({ memories: result.memories, isLoading: false });
    } catch (e) {
      set({ error: String(e), isLoading: false });
    }
  },

  loadArchived: async () => {
    try {
      const result = await memoryApi.archived();
      set({ archived: result.memories });
    } catch (e) {
      // Best-effort: the archived list is an audit view, and failing to load it
      // must never break the panel that shows the user's actual facts.
      set({ archived: [] });
    }
  },

  restoreFact: async (id: string) => {
    await memoryApi.restore(id);
    // Reload both: the fact leaves the archive AND rejoins the live list.
    await Promise.all([get().loadArchived(), get().loadFacts()]);
  },

  searchFacts: async (q: string) => {
    if (!q.trim()) {
      get().loadFacts();
      return;
    }
    set({ isLoading: true });
    try {
      const result = await memoryApi.search(q);
      // Filter to only user/shared facts in search results
      const userFacts = result.memories.filter(
        (m) => !m.subject || m.subject === 'user' || m.subject === 'shared'
      );
      set({ memories: userFacts, isLoading: false });
    } catch (e) {
      set({ error: String(e), isLoading: false });
    }
  },

  addFact: async (content: string, category?: string) => {
    await memoryApi.create(content, category);
    await get().loadFacts();
  },

  deleteFact: async (id: string) => {
    await memoryApi.delete(id);
    set((state) => ({ memories: state.memories.filter((m) => m.id !== id) }));
  },

  setSearchQuery: (q) => set({ searchQuery: q }),
}));
