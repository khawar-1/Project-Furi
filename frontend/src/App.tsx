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
import { useUIStore } from '@/stores/uiStore';
import type { ActivePanel } from '@/types';

function PanelContent({ panel }: { panel: ActivePanel }) {
  switch (panel) {
    case 'chat':
      return <ChatPanel />;
    case 'memory':
      return <MemoryExplorer />;
    case 'contacts':
      return <ContactsPanel />;
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
    tools: 'Tool Execution Log',
    voice: 'Voice Controls',
  };

  const phases: Record<ActivePanel, string> = {
    chat: '1',
    memory: '2',
    contacts: '2',
    timeline: '3',
    tools: '5',
    voice: '6',
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
