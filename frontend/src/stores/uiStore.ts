/**
 * Jarvis OS — UI State (Zustand)
 * Manages global UI state: active panel, sidebar, backend connection.
 */
import { create } from 'zustand';
import type { ActivePanel, HealthResponse, HealthStatus } from '@/types';
import { healthApi } from '@/lib/api';

interface UIState {
  // Navigation
  activePanel: ActivePanel;
  isSidebarCollapsed: boolean;

  // Backend connection
  backendStatus: HealthStatus;
  healthData: HealthResponse | null;
  isCheckingHealth: boolean;

  // Voice mode
  isVoiceMode: boolean;
  isListening: boolean;

  // Actions
  setActivePanel: (panel: ActivePanel) => void;
  toggleSidebar: () => void;
  checkBackendHealth: () => Promise<void>;
  setVoiceMode: (enabled: boolean) => void;
  setListening: (listening: boolean) => void;
}

export const useUIStore = create<UIState>((set) => ({
  // ---- Initial State
  activePanel: 'chat',
  isSidebarCollapsed: false,
  backendStatus: 'degraded',
  healthData: null,
  isCheckingHealth: false,
  isVoiceMode: false,
  isListening: false,

  // ---- Actions
  setActivePanel: (panel: ActivePanel) => {
    set({ activePanel: panel });
  },

  toggleSidebar: () => {
    set((state) => ({ isSidebarCollapsed: !state.isSidebarCollapsed }));
  },

  checkBackendHealth: async () => {
    set({ isCheckingHealth: true });
    try {
      const health = await healthApi.check();
      set({
        backendStatus: health.status,
        healthData: health,
        isCheckingHealth: false,
      });
    } catch {
      set({
        backendStatus: 'error',
        healthData: null,
        isCheckingHealth: false,
      });
    }
  },

  setVoiceMode: (enabled: boolean) => {
    set({ isVoiceMode: enabled });
  },

  setListening: (listening: boolean) => {
    set({ isListening: listening });
  },
}));
