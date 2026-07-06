/**
 * Jarvis OS — Typed API Client (Phase 2)
 * All backend communication goes through this module.
 */
import type {
  ChatMessage,
  ChatRequest,
  Contact,
  Episode,
  HealthResponse,
  MemorySearchResult,
  MemoryStats,
  Preference,
  SemanticMemory,
  StreamChunk,
} from '@/types';

// Resolve backend URL
function getBaseUrl(): string {
  if (typeof window !== 'undefined' && window.__BACKEND_URL__) {
    return window.__BACKEND_URL__;
  }
  return 'http://localhost:8000';
}

// ============================================================
// Base fetch with error handling
// ============================================================
async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const url = `${getBaseUrl()}${path}`;
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  });

  if (!response.ok) {
    const text = await response.text().catch(() => response.statusText);
    throw new Error(`API Error ${response.status}: ${text}`);
  }

  return response.json() as Promise<T>;
}

// ============================================================
// Health
// ============================================================
export const healthApi = {
  check: (): Promise<HealthResponse> => apiFetch<HealthResponse>('/health'),
};

// ============================================================
// Chat
// ============================================================
export const chatApi = {
  streamChat: async (
    request: ChatRequest,
    onChunk: (chunk: StreamChunk) => void,
    onDone: (sessionId: string) => void,
    onError: (error: string) => void
  ): Promise<void> => {
    const url = `${getBaseUrl()}/chat/stream`;
    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
      });

      if (!response.ok) {
        const text = await response.text().catch(() => response.statusText);
        throw new Error(`Stream error ${response.status}: ${text}`);
      }

      const reader = response.body?.getReader();
      if (!reader) throw new Error('ReadableStream not available');

      const decoder = new TextDecoder();
      let buffer = '';
      let lastSessionId = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() ?? '';

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed.startsWith('data:')) continue;
          const jsonStr = trimmed.slice(5).trim();
          if (!jsonStr) continue;

          try {
            const chunk = JSON.parse(jsonStr) as StreamChunk;
            if (chunk.session_id) lastSessionId = chunk.session_id;
            if (chunk.done) {
              if (chunk.delta) onChunk(chunk);
              onDone(lastSessionId);
            } else {
              onChunk(chunk);
            }
          } catch { /* Malformed chunk */ }
        }
      }
    } catch (error) {
      onError(error instanceof Error ? error.message : 'Unknown streaming error');
    }
  },

  getSessionMessages: (sessionId: string): Promise<ChatMessage[]> =>
    apiFetch<ChatMessage[]>(`/chat/sessions/${sessionId}/messages`),
};

// ============================================================
// Memory
// ============================================================
export const memoryApi = {
  list: (params?: { limit?: number; category?: string; subject?: string }): Promise<MemorySearchResult> => {
    const query = new URLSearchParams();
    if (params?.limit) query.set('limit', String(params.limit));
    if (params?.category) query.set('category', params.category);
    if (params?.subject) query.set('subject', params.subject);
    return apiFetch<MemorySearchResult>(`/memory?${query.toString()}`);
  },

  search: (q: string): Promise<{ memories: SemanticMemory[]; episodes: Episode[] }> =>
    apiFetch(`/memory/search?q=${encodeURIComponent(q)}`),

  create: (content: string, category?: string): Promise<SemanticMemory> =>
    apiFetch('/memory', {
      method: 'POST',
      body: JSON.stringify({ content, category: category || 'fact', source: 'explicit', confidence: 1.0 }),
    }),

  delete: (id: string): Promise<{ deleted: string }> =>
    apiFetch(`/memory/${id}`, { method: 'DELETE' }),

  stats: (): Promise<MemoryStats> => apiFetch<MemoryStats>('/memory/stats'),
};

// ============================================================
// Contacts
// ============================================================
export const contactsApi = {
  list: (): Promise<Contact[]> => apiFetch<Contact[]>('/api/contacts'),

  get: (id: string): Promise<Contact> => apiFetch<Contact>(`/api/contacts/${id}`),

  create: (payload: Partial<Contact> & { name: string }): Promise<Contact> =>
    apiFetch('/api/contacts', { method: 'POST', body: JSON.stringify(payload) }),

  update: (id: string, payload: Partial<Contact>): Promise<Contact> =>
    apiFetch(`/api/contacts/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),

  delete: (id: string): Promise<{ deleted: string }> =>
    apiFetch(`/api/contacts/${id}`, { method: 'DELETE' }),

  resolve: (name: string): Promise<{ status: string; contact?: Contact; matches?: Contact[] }> =>
    apiFetch(`/api/contacts/resolve/${encodeURIComponent(name)}`),
};

// ============================================================
// Episodes
// ============================================================
export const episodesApi = {
  list: (limit = 30): Promise<Episode[]> =>
    apiFetch<Episode[]>(`/api/episodes?limit=${limit}`),

  search: (q: string): Promise<Episode[]> =>
    apiFetch<Episode[]>(`/api/episodes/search?q=${encodeURIComponent(q)}`),
};

// ============================================================
// Preferences
// ============================================================
export const preferencesApi = {
  list: (): Promise<Preference[]> => apiFetch<Preference[]>('/api/preferences'),

  delete: (id: string): Promise<{ deleted: string }> =>
    apiFetch(`/api/preferences/${id}`, { method: 'DELETE' }),
};
