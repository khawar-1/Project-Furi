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
  isLoading: boolean;
  searchQuery: string;
  error: string | null;

  // Actions
  loadFacts: () => Promise<void>;
  searchFacts: (q: string) => Promise<void>;
  addFact: (content: string, category?: string) => Promise<void>;
  deleteFact: (id: string) => Promise<void>;
  setSearchQuery: (q: string) => void;
}

export const useMemoryStore = create<AboutMeState>((set, get) => ({
  memories: [],
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
