/**
 * Jarvis OS — Shared TypeScript Types (Phase 2)
 * All types used across the frontend application.
 */

// ============================================================
// Chat
// ============================================================
export type MessageRole = 'user' | 'assistant' | 'system';

export interface ChatMessage {
  id: string;
  role: MessageRole;
  content: string;
  createdAt: Date;
  isStreaming?: boolean;
  model?: string;
  provider?: string;
  /** Agent plan attached to this assistant message (Phase 3 task turns). */
  plan?: AgentPlan;
  /** True if the plan originally paused at the approval gate — the card
   *  carries the approval UI, so the duplicate text bubble is hidden. */
  planNeededApproval?: boolean;
  /** An approve/cancel call for this message's plan is in flight. */
  planResponding?: boolean;
  /** Error from the approve/cancel call (e.g. plan expired). */
  planError?: string | null;
  /** A mid-plan cancel was requested for this message's background task
   *  (Phase 4, Part 6) — the card shows "Cancelling…" until the cancelled
   *  "task" push event resolves it. */
  planCancelRequested?: boolean;
}

export interface StreamChunk {
  delta: string;
  done: boolean;
  session_id?: string;
  model?: string;
  provider?: string;
  /** "plan" marks the special agent-plan message type (Phase 3). */
  type?: string;
  plan?: AgentPlan;
}

export interface ChatRequest {
  messages: Array<{ role: MessageRole; content: string }>;
  session_id?: string;
  stream?: boolean;
  provider?: string;
  model?: string;
}

// ============================================================
// LLM Providers
// ============================================================
export type LLMProviderName = 'gemini' | 'groq' | 'openrouter' | 'ollama';

export interface ProviderInfo {
  name: LLMProviderName;
  displayName: string;
  model: string;
  isAvailable: boolean;
}

// ============================================================
// Memory
// ============================================================
export type MemoryCategory = 'work' | 'personal' | 'preference' | 'skill' | 'fact' | 'other';
export type MemorySource = 'inferred' | 'explicit' | 'extracted';

export interface SemanticMemory {
  id: string;
  content: string;
  category: MemoryCategory | null;
  source: MemorySource;
  subject: 'user' | 'contact' | 'shared';
  confidence: number;
  is_active: boolean;
  created_at: string;
}

export interface MemorySearchResult {
  memories: SemanticMemory[];
  total: number;
}

export interface MemoryStats {
  semantic_memories: number;
  contacts: number;
  episodes: number;
  preferences: number;
}

// ============================================================
// Contacts (Relationship Memory)
// ============================================================
export type RelationshipType =
  | 'client'
  | 'friend'
  | 'colleague'
  | 'recruiter'
  | 'family'
  | 'mentor'
  | 'other';

export interface ContactInteraction {
  id: string;
  description: string;
  category?: string;
  interaction_date: string;
}

export interface Contact {
  id: string;
  name: string;
  email: string | null;
  phone: string | null;
  organization: string | null;
  relationship_type: RelationshipType | null;
  skills?: string[];
  birthday?: string | null;
  important_dates?: Record<string, string> | null;
  interaction_count: number;
  last_interaction: string | null;
  created_at: string;
  updated_at: string;
  interactions?: ContactInteraction[];
}

// ============================================================
// Episodes
// ============================================================
export interface Episode {
  id: string;
  title: string;
  summary: string;
  episode_type: string;
  occurred_at: string;
  created_at: string;
}

// ============================================================
// Preferences
// ============================================================
export interface Preference {
  id: string;
  key: string;
  value: string;
  description: string | null;
  source: string;
  confidence: number;
  occurrence_count: number;
  created_at: string;
  updated_at: string;
}

// ============================================================
// Activity Log
// ============================================================
export type PermissionLevel = 'read' | 'write' | 'destructive';

// ============================================================
// Agent Plans (Phase 3 — serialized AgentPlan from the backend)
// ============================================================
export type PlanStatus =
  | 'executing'
  | 'awaiting_approval'
  | 'awaiting_choice'
  | 'completed'
  | 'failed'
  | 'cancelled';

/** A clarifying question the planner needs answered before it can continue
 *  ("three files are named notes.txt — which one?"). Answering never
 *  executes anything: any write step the answer produces still pauses for
 *  approval. */
export interface PlanQuestion {
  text: string;
  options: string[];
}

export type PlanStepStatus =
  | 'pending'
  /** Transient (Phase 4, Part 6): set by a live "plan_step" push event while
   *  the step's tool call is in flight — the card shows a spinner. */
  | 'running'
  | 'completed'
  | 'failed'
  | 'skipped';

export interface PlanStepResult {
  success: boolean;
  output: unknown;
  error: string | null;
}

export interface PlanStep {
  id: string;
  description: string;
  tool: string;
  parameters: Record<string, unknown>;
  permission_level: PermissionLevel;
  requires_approval: boolean;
  status: PlanStepStatus;
  result: PlanStepResult | null;
  /** Code-derived verbatim rendering of what the step will do (exact
   *  command / paths). Null for READ steps. Never written by the LLM. */
  action_detail: string | null;
}

export interface AgentPlan {
  id: string;
  goal: string;
  session_id: string | null;
  /** Set when the plan belongs to a background Task (Phase 4, Part 5):
   *  approving it resumes execution in the background, and the outcome
   *  arrives as a "task" push event instead of in the approve response. */
  task_id?: string | null;
  steps: PlanStep[];
  status: PlanStatus;
  message: string | null;
  created_at: string;
  /** Convenience flag from the backend: status === "awaiting_approval". */
  requires_approval: boolean;
  /** Open clarifying question when status === "awaiting_choice". */
  question: PlanQuestion | null;
  /** Readable outcome of an INLINE plan that reached a terminal state via
   *  the approve/choose endpoints (clicked option / Approve button). The
   *  store appends it as an assistant message — without it the answer to
   *  "…then tell me how many" was never delivered (live bug 2026-07-12).
   *  Null while paused/cancelled (the card carries those states live). */
  outcome_text?: string | null;
}

/** Payload of a "task" push event (Phase 4, Part 5) — a background task
 *  paused for approval/an answer, or finished. title/body feed the native
 *  toast; plan lets a live window render the approval card in chat. */
export interface TaskEventPayload {
  task_id?: unknown;
  status?: unknown;
  session_id?: unknown;
  goal?: unknown;
  title?: unknown;
  body?: unknown;
  plan?: unknown;
}

/** Payload of a "plan_step" push event (Phase 4, Part 6) — one step of an
 *  executing plan changed status (running → completed/failed). A live
 *  PlanCard ticks its rows from these; they are best-effort narration, the
 *  plan carried by the eventual "task"/approve response stays the truth. */
export interface PlanStepEventPayload {
  plan_id?: unknown;
  task_id?: unknown;
  session_id?: unknown;
  step_id?: unknown;
  step_index?: unknown;
  step_count?: unknown;
  status?: unknown;
  description?: unknown;
  tool?: unknown;
  permission_level?: unknown;
  error?: unknown;
}

export interface ActivityEntry {
  id: string;
  session_id: string | null;
  tool_name: string;
  action: string;
  parameters: Record<string, unknown> | string | null;
  result_summary: string | null;
  success: boolean;
  permission_level: PermissionLevel;
  duration_ms: number | null;
  created_at: string;
}

// ============================================================
// Push Channel (Phase 4 — server-initiated events over /ws)
// ============================================================
/** The envelope every server-pushed event uses. New event types are ADDED
 *  over time; the dispatcher ignores types it has no handler for, so an
 *  older frontend never breaks on a newer backend. */
export interface PushEvent {
  type: string;
  payload: Record<string, unknown>;
  ts: string;
}

// ============================================================
// Reminders (Phase 4, Part 4 — "remind me at 6 to call Jamil")
// ============================================================
export type ReminderStatus = 'pending' | 'fired' | 'cancelled';

export interface Reminder {
  id: string;
  text: string;
  session_id: string | null;
  due_at: string;
  status: ReminderStatus;
  job_id: string | null;
  created_at: string;
  fired_at: string | null;
}

// ============================================================
// Routines (Phase 6, Part 5 — teachable procedural memory)
// ============================================================
export type RoutineScheduleType = 'interval' | 'daily' | 'weekly' | null;

export interface RoutineSchedule {
  schedule_type: RoutineScheduleType;
  schedule_hour: number;
  schedule_minute: number;
  schedule_weekday: number | null;
  schedule_interval_minutes: number | null;
}

export interface Routine {
  id: string;
  name: string;
  normalized_name: string;
  goal_template: string;
  is_active: boolean;
  // Schedule (Phase 10.2 — scheduled routines). next_run_at is server-computed.
  schedule_type: RoutineScheduleType;
  schedule_hour: number;
  schedule_minute: number;
  schedule_weekday: number | null;
  schedule_interval_minutes: number | null;
  next_run_at: string | null;
  created_at: string;
  updated_at: string;
}

// ============================================================
// Goal threads (Phase 11.3 — ongoing-concern tracking)
// ============================================================
export type GoalThreadStatus = 'open' | 'resolved' | 'dropped';

export interface GoalThread {
  id: string;
  title: string;
  description: string | null;
  status: GoalThreadStatus;
  contact_id: string | null;
  event_date: string | null;
  next_check_at: string | null;
  last_nudged_at: string | null;
  source: string;
  created_at: string;
  updated_at: string;
}

// ============================================================
// Health / Backend Status
// ============================================================
export type HealthStatus = 'ok' | 'degraded' | 'error';

export interface ComponentHealth {
  status: HealthStatus;
  detail: string | null;
}

export interface HealthResponse {
  status: HealthStatus;
  version: string;
  provider: string;
  model: string;
  components: Record<string, ComponentHealth>;
  timestamp: string;
}

// ============================================================
// UI State
// ============================================================
export type ActivePanel =
  | 'chat'
  | 'memory'
  | 'contacts'
  | 'timeline'
  | 'reminders'
  | 'routines'
  | 'initiative'
  | 'threads'
  | 'tools'
  | 'voice'
  | 'settings';

// ============================================================
// Integrations (Phase 5, Part 1 — Google account)
// ============================================================
export interface GoogleIntegrationStatus {
  configured: boolean;
  connected: boolean;
  connecting: boolean;
  account_email: string | null;
  scopes: string[];
  detail: string | null;
}

export type GoogleConnectResult =
  | 'pending'
  | 'already_connected'
  | 'in_progress'
  | 'not_configured';

// Phase 5 Part 6 — daily briefing settings
export interface BriefingSettings {
  enabled: boolean;
  time: string; // "HH:MM" local
  next_run_at: string | null;
}

// Phase 6 Part 2 — semantic file index settings
export interface FileIndexStatus {
  indexed_files: number;
  indexed_chunks: number;
  last_indexed_at: string | null;
  indexing: boolean;
}

export interface FileIndexSettings {
  enabled: boolean;
  folders: string[];
  exclusions: string[];
  interval_minutes: number;
  status: FileIndexStatus;
}

/** Phase 6 Part 6 — a learned save/move destination (File Intelligence). */
export interface FrequentFolder {
  folder: string;
  count: number;
  last_used: string | null;
}

// ============================================================
// Voice (Phase 7 — push-to-talk)
// ============================================================
/** The STT model lifecycle (the first load doubles as a large download). */
export interface SttStatus {
  status: 'not_loaded' | 'loading' | 'ready' | 'error';
  model: string | null;
  error: string | null;
  /** The device the model actually loaded on ("cpu"/"cuda"/null before load). */
  device?: string | null;
}

/** The TTS engine lifecycle (Kokoro). The model loads once and is
 *  voice-independent (preset voices are selected per-synth); `voice` is unused
 *  here (the configured preset is reported by /status as configured_voice). The
 *  download is opaque, so `progress` is always null → the card shows an
 *  indeterminate bar. */
export interface TtsStatus {
  status: 'not_loaded' | 'loading' | 'ready' | 'error';
  voice: string | null;
  error: string | null;
  progress: { downloaded: number; total: number | null; file: string } | null;
  /** The inference device (always "cpu" for the Kokoro/onnxruntime engine). */
  device?: string;
}

/** A selectable Kokoro preset voice (id + human label). */
export interface VoiceOption {
  id: string;
  label: string;
}

/** GET/PUT /api/settings/voice — config + live model state in one fetch. */
export interface VoiceSettings {
  enabled: boolean;
  stt_model: string;
  review_before_send: boolean;
  /** Part 3: speech output master switch (still gated on `enabled`). */
  output_enabled: boolean;
  /** The Kokoro preset voice id (one of `voices`). */
  voice: string;
  /** Part 5: speak server-initiated pushes (reminders, briefings). */
  speak_proactive: boolean;
  /** Part 4: speak TYPED turns too (voice-initiated turns always speak). */
  speak_all_responses: boolean;
  /** Part 5: start a hands-free recording when the global hotkey summons
   *  the window (opt-in). */
  listen_on_summon: boolean;
  /** Speaking speed (1.0 = natural; 0.5..2.0). */
  tts_speed: number;
  /** Where each engine runs ("auto"/"cpu"/"cuda"). */
  stt_device: string;
  tts_device: string;
  /** faster-whisper precision ("auto" derives from the device). */
  stt_compute_type: string;
  /** Phase 12.1: after a spoken reply, re-open a short hands-free window so
   *  the user can talk back without re-triggering (opt-in). */
  continuous_conversation: boolean;
  /** Phase 12.2: always-on on-device "Hey Jarvis" detection in the renderer
   *  (opt-in; raw audio never leaves the machine). */
  wake_word: boolean;
  stt_models: string[];
  /** The selectable preset voices. */
  voices: VoiceOption[];
  /** The selectable device options and whisper compute types. */
  devices: string[];
  stt_compute_types: string[];
  stt_status: SttStatus;
  tts_status: TtsStatus;
}

/** POST /api/voice/transcribe response. */
export interface TranscribeResult {
  text: string;
  language: string | null;
  duration: number;
}

// ============================================================
// Context Layer (Phase 8)
// ============================================================
/** GET/PUT /api/context/settings — the privacy-first sensing config. */
export interface ContextSettings {
  enabled: boolean;              // master kill switch
  device_sensing: boolean;
  screen_ocr: boolean;           // OCR capability (capture also armed per-session)
  ocr_interval_seconds: number;
  idle_threshold_seconds: number;
  affective_sensing: boolean;    // Phase 13 — coarse load read (separate opt-in)
}

/** GET /api/context/status — cheap sensing status for the indicator. */
export interface ContextStatus {
  enabled: boolean;
  device_sensing: boolean;
  screen_ocr: boolean;
  affective_sensing: boolean;
  device_fresh: boolean;
  ocr_fresh: boolean;
}

/** The coarse affective load read (Phase 13) — null unless affective sensing is
 *  on and a fresh signal contributed. `signals` echoes the raw inputs. */
export interface UserState {
  load: 'calm' | 'steady' | 'busy' | 'stressed';
  confidence: number;
  signals: Record<string, number>;
}

/** GET /api/context/world — the aggregated world model (UI audit surface). */
export interface WorldModel {
  presence: 'active' | 'idle' | 'away' | 'unknown';
  active_app: string | null;
  window_title: string | null;
  idle_seconds: number | null;
  next_calendar_event: {
    summary?: string;
    when?: string;
    location?: string;
  } | null;
  unread: { count: number; has_urgent: boolean } | null;
  recent_file_focus: { filename: string; path: string; modified: string } | null;
  on_screen_context: string | null;
  user_state: UserState | null;
  sensing: {
    enabled: boolean;
    device_sensing: boolean;
    screen_ocr: boolean;
    affective_sensing: boolean;
    device_fresh: boolean;
    ocr_fresh: boolean;
  };
  captured_at: string;
}

// ============================================================
// Initiative Engine (Phase 9 — proactive suggestions)
// ============================================================
/** The autonomy ceiling the user sets — how far Jarvis may go on its own. */
export type AutonomyLevel = 'off' | 'suggest' | 'ask' | 'act';

/** What the policy classified a surfaced suggestion as. */
export type SuggestionAutonomy = 'suggest' | 'ask' | 'act';

export type SuggestionPriority = 'low' | 'normal' | 'high';

export type SuggestionStatus =
  | 'pending'
  | 'accepted'
  | 'dismissed'
  | 'acted'
  | 'expired';

/** One proactive suggestion from the initiative heartbeat (the feed row). */
export interface Suggestion {
  id: string;
  session_id: string | null;
  category: string;
  title: string;
  body: string;
  rationale: string; // the "why it matters"
  autonomy: SuggestionAutonomy;
  priority: SuggestionPriority;
  goal: string | null;
  status: SuggestionStatus;
  task_id: string | null;
  created_at: string;
  updated_at: string;
  expires_at: string | null;
}

/** GET/PUT /api/initiative/settings — the engine's governor config. */
export interface InitiativeSettings {
  enabled: boolean;
  autonomy: AutonomyLevel;
  interval_minutes: number;
  daily_budget: number;
  quiet_start_hour: number;
  quiet_end_hour: number;
  min_gap_minutes: number;
  autonomy_levels: AutonomyLevel[];
  next_run_at: string | null;
}

// ============================================================
// Electron Bridge (exposed by preload.ts)
// ============================================================
export interface JarvisElectronAPI {
  getBackendUrl: () => string;
  openExternal: (url: string) => void;
  minimizeWindow: () => void;
  maximizeWindow: () => void;
  closeWindow: () => void;
  getPlatform: () => string;
  getAppVersion: () => Promise<string>;
  /** Show a native OS notification (Phase 4, Part 3). The main process
   *  creates the toast; clicking it summons the window. */
  notify: (title: string, body: string) => void;
  onBackendReady: (callback: () => void) => void;
  onBackendError: (callback: (error: string) => void) => void;
  /** The global hotkey summoned the window (Phase 7, Part 5) — the renderer
   *  may start an opt-in hands-free recording in response. */
  onSummoned: (callback: () => void) => void;
  /** Arm/disarm per-session screen OCR sensing (Phase 8). The capture loop
   *  runs in the main process; no image ever crosses the bridge. */
  startScreenSensing: () => void;
  stopScreenSensing: () => void;
  removeAllListeners: (channel: string) => void;
}

// Augment the global Window interface
declare global {
  interface Window {
    jarvis: JarvisElectronAPI;
    __BACKEND_URL__: string;
    /** Static API auth token, injected by preload from the main process
     *  (which read ~/.jarvis/auth_token). Absent in plain-browser dev —
     *  getAuthToken() falls back to VITE_JARVIS_TOKEN there. */
    __JARVIS_TOKEN__?: string;
  }
}
