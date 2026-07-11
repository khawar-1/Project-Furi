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
  removeAllListeners: (channel: string) => void;
}

// Augment the global Window interface
declare global {
  interface Window {
    jarvis: JarvisElectronAPI;
    __BACKEND_URL__: string;
  }
}
