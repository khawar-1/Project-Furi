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
}

export interface StreamChunk {
  delta: string;
  done: boolean;
  session_id?: string;
  model?: string;
  provider?: string;
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

export interface ActivityEntry {
  id: string;
  sessionId: string | null;
  toolName: string;
  action: string;
  parameters: string | null;
  resultSummary: string | null;
  success: boolean;
  permissionLevel: PermissionLevel;
  durationMs: number | null;
  createdAt: Date;
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
  | 'tools'
  | 'voice';

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
