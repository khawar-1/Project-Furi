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
  ContextSettings,
  ContextStatus,
  Episode,
  FileIndexSettings,
  FileIndexStatus,
  FrequentFolder,
  GoogleConnectResult,
  GoogleIntegrationStatus,
  HealthResponse,
  InitiativeSettings,
  MemorySearchResult,
  MemoryStats,
  Preference,
  Reminder,
  ReminderStatus,
  Routine,
  Suggestion,
  SuggestionStatus,
  SemanticMemory,
  SttStatus,
  StreamChunk,
  TranscribeResult,
  TtsStatus,
  VoiceSettings,
  WorldModel,
} from '@/types';

// Resolve backend URL (also used by lib/push.ts to derive the ws:// URL)
export function getBaseUrl(): string {
  if (typeof window !== 'undefined' && window.__BACKEND_URL__) {
    return window.__BACKEND_URL__;
  }
  return 'http://localhost:8000';
}

// The static API auth token every backend call must carry (the backend's
// AuthMiddleware 401s without it). In Electron, preload injects it as
// window.__JARVIS_TOKEN__; in plain-browser dev put the value of
// ~/.jarvis/auth_token in frontend/.env.local as VITE_JARVIS_TOKEN.
// Also used by lib/push.ts (WebSockets can't set headers → ?token= param).
export function getAuthToken(): string {
  if (typeof window !== 'undefined' && window.__JARVIS_TOKEN__) {
    return window.__JARVIS_TOKEN__;
  }
  return import.meta.env.VITE_JARVIS_TOKEN ?? '';
}

function authHeaders(): Record<string, string> {
  const token = getAuthToken();
  return token ? { 'X-Jarvis-Token': token } : {};
}

// ============================================================
// Base fetch with error handling
// ============================================================
async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const url = `${getBaseUrl()}${path}`;
  const response = await fetch(url, {
    ...options,
    // Auth header spread LAST so no caller can accidentally drop it.
    headers: { 'Content-Type': 'application/json', ...options.headers, ...authHeaders() },
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
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
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
// Initiative Engine (Phase 9 — proactive suggestions)
// ============================================================
export const initiativeApi = {
  listSuggestions: (status?: SuggestionStatus): Promise<Suggestion[]> => {
    const query = status ? `?status=${status}` : '';
    return apiFetch<Suggestion[]>(`/api/initiative/suggestions${query}`);
  },

  /** Accept a suggestion. If it carries a goal, an approval-gated background
   *  Task starts (the outcome arrives as a push/toast + PlanCard in chat). */
  accept: (id: string): Promise<Suggestion> =>
    apiFetch<Suggestion>(`/api/initiative/suggestions/${id}/accept`, { method: 'POST' }),

  dismiss: (id: string): Promise<Suggestion> =>
    apiFetch<Suggestion>(`/api/initiative/suggestions/${id}/dismiss`, { method: 'POST' }),

  getSettings: (): Promise<InitiativeSettings> =>
    apiFetch<InitiativeSettings>('/api/initiative/settings'),

  updateSettings: (update: {
    enabled: boolean;
    autonomy: string;
    interval_minutes: number;
    daily_budget: number;
    quiet_start_hour: number;
    quiet_end_hour: number;
    min_gap_minutes: number;
  }): Promise<InitiativeSettings> =>
    apiFetch<InitiativeSettings>('/api/initiative/settings', {
      method: 'PUT',
      body: JSON.stringify(update),
    }),

  /** Run one initiative pass now (respects the daily budget). */
  runNow: (): Promise<{ surfaced: number }> =>
    apiFetch('/api/initiative/run-now', { method: 'POST' }),
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
// Context Layer (Phase 8) — settings, status, world model
// ============================================================
export const contextApi = {
  getSettings: (): Promise<ContextSettings> =>
    apiFetch<ContextSettings>('/api/context/settings'),

  updateSettings: (update: ContextSettings): Promise<ContextSettings> =>
    apiFetch<ContextSettings>('/api/context/settings', {
      method: 'PUT',
      body: JSON.stringify(update),
    }),

  /** Cheap sensing status — drives the StatusBar indicator. */
  getStatus: (): Promise<ContextStatus> =>
    apiFetch<ContextStatus>('/api/context/status'),

  /** The aggregated world model — the "what Jarvis currently sees" audit. */
  getWorld: (): Promise<WorldModel> => apiFetch<WorldModel>('/api/context/world'),
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
// Voice (Phase 7 — push-to-talk)
// ============================================================

/** The full PUT /api/settings/voice body — the backend persists every field,
 *  so a partial body would silently reset the omitted ones to defaults. */
export interface VoiceUpdateBody {
  enabled: boolean;
  stt_model: string;
  review_before_send: boolean;
  output_enabled: boolean;
  voice: string;
  speak_proactive: boolean;
  speak_all_responses: boolean;
  listen_on_summon: boolean;
  tts_speed: number;
  stt_device: string;
  tts_device: string;
  stt_compute_type: string;
}

/** Build the complete PUT body from the current settings plus a patch — the
 *  ONE place the field list lives, so every caller (ChatPanel's speaker
 *  toggle, the Settings VoiceCard) inherits a new field automatically
 *  instead of each hand-copying the shape (the drift-risk lesson). */
export function voiceUpdatePayload(
  settings: VoiceSettings,
  patch: Partial<VoiceUpdateBody> = {}
): VoiceUpdateBody {
  return {
    enabled: settings.enabled,
    stt_model: settings.stt_model,
    review_before_send: settings.review_before_send,
    output_enabled: settings.output_enabled,
    voice: settings.voice,
    speak_proactive: settings.speak_proactive,
    speak_all_responses: settings.speak_all_responses,
    listen_on_summon: settings.listen_on_summon,
    tts_speed: settings.tts_speed,
    stt_device: settings.stt_device,
    tts_device: settings.tts_device,
    stt_compute_type: settings.stt_compute_type,
    ...patch,
  };
}

export const voiceApi = {
  getSettings: (): Promise<VoiceSettings> =>
    apiFetch<VoiceSettings>('/api/settings/voice'),

  /** PUT enabling voice (or switching models/voices) kicks the model
   *  downloads immediately server-side; poll status() through them.
   *  Build the body with voiceUpdatePayload() — never by hand. */
  updateSettings: (update: VoiceUpdateBody): Promise<VoiceSettings> =>
    apiFetch<VoiceSettings>('/api/settings/voice', {
      method: 'PUT',
      body: JSON.stringify(update),
    }),

  /** Purely local on the backend (no I/O) — safe to poll while a model loads. */
  status: (): Promise<{
    enabled: boolean;
    stt: SttStatus & { configured_model: string };
    tts: TtsStatus & { configured_voice: string; output_enabled: boolean };
  }> => apiFetch('/api/voice/status'),

  /** Synthesize one sentence to a WAV blob (Part 4's playback queue).
   *  Bypasses apiFetch (binary response; the transcribe precedent). Resolves
   *  null on 204 — the text sanitized to nothing (pure markdown scaffolding);
   *  the caller just skips it. Throws the backend's human-readable `detail`
   *  otherwise (voice/output disabled, engine still loading, …). */
  speak: async (text: string, signal?: AbortSignal): Promise<Blob | null> => {
    const response = await fetch(`${getBaseUrl()}/api/voice/speak`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ text }),
      signal,
    });
    if (response.status === 204) return null;
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = (await response.json()) as { detail?: string };
        if (body.detail) detail = body.detail;
      } catch { /* non-JSON error body */ }
      throw new Error(detail);
    }
    return response.blob();
  },

  /** Synthesize one sentence as a LIVE PCM stream — audio chunks arrive while
   *  the sentence is still being synthesized (~1s to first sound instead of
   *  the whole synthesis). Raw s16le mono at `sampleRate`; the playback queue
   *  schedules chunks with Web Audio. Resolves null on 204 (sanitized to
   *  nothing); throws the backend's `detail` otherwise — callers fall back
   *  to the blob speak() on failure. */
  speakStream: async (
    text: string,
    signal?: AbortSignal
  ): Promise<{ sampleRate: number; body: ReadableStream<Uint8Array> } | null> => {
    const response = await fetch(`${getBaseUrl()}/api/voice/speak/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ text }),
      signal,
    });
    if (response.status === 204) return null;
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = (await response.json()) as { detail?: string };
        if (body.detail) detail = body.detail;
      } catch { /* non-JSON error body */ }
      throw new Error(detail);
    }
    if (!response.body) throw new Error('Streaming not supported by this environment.');
    const sampleRate = parseInt(response.headers.get('X-Sample-Rate') || '24000', 10);
    return { sampleRate: Number.isFinite(sampleRate) ? sampleRate : 24000, body: response.body };
  },

  /** Transcribe one recorded utterance. Multipart, so this bypasses apiFetch
   *  (the browser must set the multipart boundary itself — a forced JSON
   *  Content-Type would break the upload). Errors surface the backend's
   *  human-readable `detail` ("model not ready yet", "voice disabled", ...). */
  transcribe: async (audio: Blob, signal?: AbortSignal): Promise<TranscribeResult> => {
    const form = new FormData();
    form.append('file', audio, 'utterance.webm');
    const response = await fetch(`${getBaseUrl()}/api/voice/transcribe`, {
      method: 'POST',
      // Auth header ONLY — never a Content-Type here: the browser must set
      // the multipart boundary itself or the upload breaks.
      headers: authHeaders(),
      body: form,
      signal,
    });
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = (await response.json()) as { detail?: string };
        if (body.detail) detail = body.detail;
      } catch { /* non-JSON error body */ }
      throw new Error(detail);
    }
    return response.json() as Promise<TranscribeResult>;
  },
};

// ============================================================
// Preferences
// ============================================================
export const preferencesApi = {
  list: (): Promise<Preference[]> => apiFetch<Preference[]>('/api/preferences'),

  delete: (id: string): Promise<{ deleted: string }> =>
    apiFetch(`/api/preferences/${id}`, { method: 'DELETE' }),
};
