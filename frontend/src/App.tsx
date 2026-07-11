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
import { SettingsPanel } from '@/components/settings/SettingsPanel';
import { useUIStore } from '@/stores/uiStore';
import { connectPush, disconnectPush, onPush } from '@/lib/push';
import { initNotifications } from '@/lib/notifications';
import { useChatStore } from '@/stores/chatStore';
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

  // Phase 4: the push channel — the server can now speak first — and the
  // native-notification bridge that makes it felt while the app is in the tray.
  useEffect(() => {
    connectPush();
    const stopNotifications = initNotifications();
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
    return () => {
      stopSteps();
      stopTasks();
      stopReminders();
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
