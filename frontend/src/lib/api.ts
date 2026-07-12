/**
 * Jarvis OS — Typed API Client (Phase 2)
 * All backend communication goes through this module.
 */
import type {
  ActivityEntry,
  AgentPlan,
  BriefingSettings,
  ChatMessage,
  ChatRequest,
  Contact,
  Episode,
  FileIndexSettings,
  FileIndexStatus,
  FrequentFolder,
  GoogleConnectResult,
  GoogleIntegrationStatus,
  HealthResponse,
  MemorySearchResult,
  MemoryStats,
  Preference,
  Reminder,
  ReminderStatus,
  Routine,
  SemanticMemory,
  StreamChunk,
} from '@/types';

// Resolve backend URL (also used by lib/push.ts to derive the ws:// URL)
export function getBaseUrl(): string {
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

  deleteInteraction: (contactId: string, interactionId: string): Promise<{ deleted: string }> =>
    apiFetch(`/api/contacts/${contactId}/interactions/${interactionId}`, { method: 'DELETE' }),

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
// Agent (Phase 3 — plan approval)
// ============================================================
export const agentApi = {
  /** Approve or cancel a plan parked at the approval gate. Consumes the
   *  pending plan — one answer per plan; returns the final plan state. */
  approve: (planId: string, approved: boolean): Promise<AgentPlan> =>
    apiFetch<AgentPlan>('/api/agent/approve', {
      method: 'POST',
      body: JSON.stringify({ plan_id: planId, approved }),
    }),

  /** Answer a plan's clarifying question ("which notes.txt?"). Also consumes
   *  the pending plan; the answer only feeds the next planning round. */
  choose: (planId: string, answer: string): Promise<AgentPlan> =>
    apiFetch<AgentPlan>('/api/agent/choose', {
      method: 'POST',
      body: JSON.stringify({ plan_id: planId, answer }),
    }),
};

// ============================================================
// Background tasks (Phase 4, Parts 5-6)
// ============================================================
export const tasksApi = {
  /** Cooperative mid-plan cancel (Part 6): sets the flag a running plan
   *  checks between steps. The step currently executing finishes; the
   *  cancelled outcome then arrives as a "task" push event. */
  cancel: (
    taskId: string
  ): Promise<{ task_id: string; status: string; accepted: boolean; detail: string }> =>
    apiFetch(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' }),
};

// ============================================================
// Activity (Phase 3 — tool execution audit trail)
// ============================================================
export const activityApi = {
  list: (limit = 100): Promise<ActivityEntry[]> =>
    apiFetch<ActivityEntry[]>(`/api/activity?limit=${limit}`),

  forSession: (sessionId: string, limit = 100): Promise<ActivityEntry[]> =>
    apiFetch<ActivityEntry[]>(
      `/api/activity/${encodeURIComponent(sessionId)}?limit=${limit}`
    ),
};

// ============================================================
// Reminders (Phase 4, Part 4)
// ============================================================
export const remindersApi = {
  list: (status?: ReminderStatus): Promise<Reminder[]> => {
    const query = status ? `?status=${status}` : '';
    return apiFetch<Reminder[]>(`/api/reminders${query}`);
  },

  cancel: (id: string): Promise<{ cancelled: boolean }> =>
    apiFetch(`/api/reminders/${id}`, { method: 'DELETE' }),
};

// ============================================================
// Routines (Phase 6, Part 5 — teachable procedural memory)
// ============================================================
export const routinesApi = {
  list: (): Promise<Routine[]> => apiFetch<Routine[]>('/api/routines'),

  create: (payload: { name: string; goal_template: string }): Promise<Routine> =>
    apiFetch<Routine>('/api/routines', { method: 'POST', body: JSON.stringify(payload) }),

  delete: (id: string): Promise<{ deleted: boolean }> =>
    apiFetch(`/api/routines/${id}`, { method: 'DELETE' }),

  /** Run a routine now — starts a background Task (approval gates still apply);
   *  the outcome arrives as a push event / toast. */
  run: (id: string, sessionId?: string): Promise<{ task_id: string; status: string }> =>
    apiFetch(`/api/routines/${id}/run`, {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId ?? null }),
    }),
};

// ============================================================
// Integrations (Phase 5, Part 1 — Google account)
// ============================================================
export const integrationsApi = {
  /** Purely local on the backend (config + token file, no network) — safe to poll. */
  googleStatus: (): Promise<GoogleIntegrationStatus> =>
    apiFetch<GoogleIntegrationStatus>('/api/integrations/google/status'),

  /** Starts the OAuth consent flow in the system browser; returns immediately.
   *  Poll googleStatus() until `connected` (or the flow times out server-side). */
  googleConnect: (): Promise<{ status: GoogleConnectResult }> =>
    apiFetch('/api/integrations/google/connect', { method: 'POST' }),

  /** Revokes at Google (best-effort) and deletes the local token. */
  googleDisconnect: (): Promise<{ disconnected: boolean; revoked: boolean }> =>
    apiFetch('/api/integrations/google/disconnect', { method: 'POST' }),
};

// ============================================================
// App settings (Phase 5, Part 6 — daily briefing)
// ============================================================
export const settingsApi = {
  getBriefing: (): Promise<BriefingSettings> =>
    apiFetch<BriefingSettings>('/api/settings/briefing'),

  updateBriefing: (update: { enabled: boolean; time: string }): Promise<BriefingSettings> =>
    apiFetch<BriefingSettings>('/api/settings/briefing', {
      method: 'PUT',
      body: JSON.stringify(update),
    }),

  /** Compose + deliver a briefing immediately ("Send now"). Read-only. */
  runBriefingNow: (): Promise<{ delivered: boolean; message: string }> =>
    apiFetch('/api/settings/briefing/run-now', { method: 'POST' }),
};

// ============================================================
// Semantic file index (Phase 6, Part 2)
// ============================================================
export const indexApi = {
  get: (): Promise<FileIndexSettings> => apiFetch<FileIndexSettings>('/api/index'),

  status: (): Promise<FileIndexStatus> => apiFetch<FileIndexStatus>('/api/index/status'),

  updateConfig: (update: {
    enabled: boolean;
    folders: string[];
    exclusions: string[];
    interval_minutes: number;
  }): Promise<FileIndexSettings> =>
    apiFetch<FileIndexSettings>('/api/index/config', {
      method: 'PUT',
      body: JSON.stringify(update),
    }),

  /** Start a background index pass. Poll status() until `indexing` is false. */
  rebuild: (full = false): Promise<{ started: boolean; indexing: boolean }> =>
    apiFetch('/api/index/rebuild', { method: 'POST', body: JSON.stringify({ full }) }),

  /** Phase 6 Part 6: the learned save/move destinations (read-only). */
  frequentFolders: (limit = 5): Promise<{ folders: FrequentFolder[] }> =>
    apiFetch<{ folders: FrequentFolder[] }>(`/api/index/frequent-folders?limit=${limit}`),
};

// ============================================================
// Preferences
// ============================================================
export const preferencesApi = {
  list: (): Promise<Preference[]> => apiFetch<Preference[]>('/api/preferences'),

  delete: (id: string): Promise<{ deleted: string }> =>
    apiFetch(`/api/preferences/${id}`, { method: 'DELETE' }),
};
