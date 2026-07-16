/**
 * Jarvis OS — Root Application
 * About Me, Contacts panels are now live.
 */
import { useEffect } from 'react';
import { Sidebar } from '@/components/layout/Sidebar';
import { StatusBar } from '@/components/layout/StatusBar';
import { ChatPanel } from '@/components/chat/ChatPanel';
import { MemoryExplorer } from '@/components/memory/MemoryExplorer';
import { ContactsPanel } from '@/components/contacts/ContactsPanel';
import { TimelinePanel } from '@/components/timeline/TimelinePanel';
import { ReminderPanel } from '@/components/reminders/ReminderPanel';
import { RoutinesPanel } from '@/components/routines/RoutinesPanel';
import { SuggestionPanel } from '@/components/initiative/SuggestionPanel';
import { SettingsPanel } from '@/components/settings/SettingsPanel';
import { useUIStore } from '@/stores/uiStore';
import { connectPush, disconnectPush, onPush } from '@/lib/push';
import { initNotifications } from '@/lib/notifications';
import { initVoiceAnnounce } from '@/lib/voiceAnnounce';
import { useChatStore } from '@/stores/chatStore';
import { useVoiceStore } from '@/stores/voiceStore';
import { useSuggestionsStore } from '@/stores/suggestionsStore';
import type { ActivePanel } from '@/types';

function PanelContent({ panel }: { panel: ActivePanel }) {
  switch (panel) {
    case 'chat':
      return <ChatPanel />;
    case 'memory':
      return <MemoryExplorer />;
    case 'contacts':
      return <ContactsPanel />;
    case 'timeline':
      return <TimelinePanel />;
    case 'reminders':
      return <ReminderPanel />;
    case 'routines':
      return <RoutinesPanel />;
    case 'initiative':
      return <SuggestionPanel />;
    case 'settings':
      return <SettingsPanel />;
    default:
      return <ComingSoonPanel panel={panel} />;
  }
}

function ComingSoonPanel({ panel }: { panel: ActivePanel }) {
  const labels: Record<ActivePanel, string> = {
    chat: 'Chat',
    memory: 'About Me',
    contacts: 'Contacts',
    timeline: 'Activity Timeline',
    reminders: 'Reminders',
    routines: 'Routines',
    initiative: 'Suggestions',
    tools: 'Tool Execution Log',
    voice: 'Voice Controls',
    settings: 'Settings',
  };

  const phases: Record<ActivePanel, string> = {
    chat: '1',
    memory: '2',
    contacts: '2',
    timeline: '3',
    reminders: '4',
    routines: '6',
    initiative: '9',
    tools: '5',
    voice: '6',
    settings: '5',
  };

  return (
    <div className="flex flex-col items-center justify-center h-full gap-4 text-center px-8">
      <div className="w-16 h-16 rounded-2xl bg-surface-2 hud-border flex items-center justify-center mb-2">
        <div className="w-6 h-6 rounded-full bg-cyan-500/20 border border-cyan-500/40" />
      </div>
      <h2 className="text-xl font-semibold text-slate-200">{labels[panel]}</h2>
      <p className="text-slate-500 text-sm max-w-sm">
        This module will be implemented in an upcoming phase.
      </p>
      <div className="px-3 py-1.5 rounded-full bg-surface-2 border border-surface-border text-xs text-muted font-mono">
        Coming in Phase {phases[panel]}
      </div>
    </div>
  );
}

export default function App() {
  const { activePanel, checkBackendHealth } = useUIStore();

  // Poll backend health every 30 seconds
  useEffect(() => {
    checkBackendHealth();
    const interval = setInterval(checkBackendHealth, 30_000);
    return () => clearInterval(interval);
  }, [checkBackendHealth]);

  // Phase 7: voice settings — one fetch so the mic button knows whether
  // voice is enabled and whether the STT model is ready. Belt: fetchSettings
  // is a one-shot and swallows failures, so if it loses the startup race with
  // a still-booting backend, retry once so voice doesn't stay permanently
  // "unavailable" (the other startup fetches self-heal via polling).
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await useVoiceStore.getState().fetchSettings();
      if (cancelled || useVoiceStore.getState().settings !== null) return;
      setTimeout(() => {
        if (!cancelled && useVoiceStore.getState().settings === null) {
          void useVoiceStore.getState().fetchSettings();
        }
      }, 2_000);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Phase 7 Part 5: the "Jarvis moment" — the global hotkey summons the
  // window AND (opt-in via listen_on_summon, checked in the store) starts a
  // hands-free recording. No-op in a plain browser (no Electron bridge).
  useEffect(() => {
    if (typeof window.jarvis?.onSummoned !== 'function') return;
    window.jarvis.onSummoned(() => {
      void useVoiceStore.getState().beginSummonListen();
    });
    return () => window.jarvis.removeAllListeners('summoned-by-hotkey');
  }, []);

  // Phase 4: the push channel — the server can now speak first — and the
  // native-notification bridge that makes it felt while the app is in the tray.
  useEffect(() => {
    connectPush();
    const stopNotifications = initNotifications();
    // Phase 7 Part 5: the voice sibling of the toast bridge — push events are
    // SPOKEN when speak_proactive is on (gating lives inside the module).
    const stopAnnounce = initVoiceAnnounce();
    // Part 4: a reminder firing while this window is open should also show
    // up live in chat, not just as a toast — if it's this session's.
    const stopReminders = onPush('reminder', (event) => {
      useChatStore.getState().receiveReminderFired(event.payload);
    });
    // Part 5: a background task pausing for approval (or finishing) renders
    // live in chat — the PlanCard the push carries IS the approval UI.
    const stopTasks = onPush('task', (event) => {
      useChatStore.getState().receiveTaskEvent(event.payload);
    });
    // Part 6: per-step narration — tick the matching PlanCard row live while
    // a plan executes (running → completed/failed).
    const stopSteps = onPush('plan_step', (event) => {
      useChatStore.getState().receiveStepEvent(event.payload);
    });
    // Phase 5 Part 6: a daily briefing fires unprompted — show it live in chat
    // if it belongs to the open session (and always as a toast via notifications).
    // receiveReminderFired is generic over {session_id, body, text} — reused as-is.
    const stopBriefing = onPush('briefing', (event) => {
      useChatStore.getState().receiveReminderFired(event.payload);
    });
    // Phase 6 Part 5: a recurring goal earns an "offer to save this as a
    // routine" — show it live in chat if it's this session (and always as a
    // toast). Same generic {session_id, body, text} handler.
    const stopRoutineOffer = onPush('routine_offer', (event) => {
      useChatStore.getState().receiveReminderFired(event.payload);
    });
    // Phase 9: a proactive suggestion — prepend it to the feed live (and it
    // toasts via notifications, with the reasoned "why it matters" framing).
    const stopSuggestion = onPush('suggestion', (event) => {
      useSuggestionsStore.getState().receiveSuggestion(event.payload);
    });
    return () => {
      stopSuggestion();
      stopRoutineOffer();
      stopBriefing();
      stopSteps();
      stopTasks();
      stopReminders();
      stopAnnounce();
      stopNotifications();
      disconnectPush();
    };
  }, []);

  return (
    <div className="flex flex-col h-screen w-screen bg-surface overflow-hidden">
      <div className="flex flex-1 overflow-hidden">
        <Sidebar />
        <main className="flex-1 flex flex-col overflow-hidden bg-surface">
          <PanelContent panel={activePanel} />
        </main>
      </div>
      <StatusBar />
    </div>
  );
}
