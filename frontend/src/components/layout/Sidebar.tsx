/**
 * Jarvis OS — Sidebar Navigation
 * HUD-style vertical icon navigation with active glow effects.
 */
import { clsx } from 'clsx';
import {
  MessageSquare,
  Brain,
  Users,
  Activity,
  Terminal,
  Mic,
  ChevronLeft,
  Zap,
} from 'lucide-react';
import { useUIStore } from '@/stores/uiStore';
import type { ActivePanel } from '@/types';

interface NavItem {
  id: ActivePanel;
  icon: React.ReactNode;
  label: string;
  phase?: number; // Which phase implements this panel
}

const navItems: NavItem[] = [
  { id: 'chat', icon: <MessageSquare size={20} />, label: 'Chat' },
  { id: 'memory', icon: <Brain size={20} />, label: 'About Me', phase: 2 },
  { id: 'contacts', icon: <Users size={20} />, label: 'Contacts', phase: 2 },
  { id: 'timeline', icon: <Activity size={20} />, label: 'Timeline', phase: 3 },
  { id: 'tools', icon: <Terminal size={20} />, label: 'Tools', phase: 3 },
  { id: 'voice', icon: <Mic size={20} />, label: 'Voice', phase: 6 },
];

export function Sidebar() {
  const { activePanel, setActivePanel, isSidebarCollapsed, toggleSidebar } = useUIStore();

  return (
    <aside
      className={clsx(
        'flex flex-col h-full bg-surface-1 border-r border-surface-border transition-all duration-300',
        isSidebarCollapsed ? 'w-[60px]' : 'w-[200px]'
      )}
    >
      {/* Logo / Brand */}
      <div
        className={clsx(
          'flex items-center h-14 px-3 border-b border-surface-border flex-shrink-0',
          isSidebarCollapsed ? 'justify-center' : 'gap-3'
        )}
      >
        <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-cyan-500 to-blue-600 flex items-center justify-center flex-shrink-0 shadow-glow-cyan">
          <Zap size={16} className="text-white" />
        </div>
        {!isSidebarCollapsed && (
          <span className="text-sm font-semibold text-slate-200 tracking-wide">
            Jarvis <span className="text-cyan-400">OS</span>
          </span>
        )}
      </div>

      {/* Navigation Items */}
      <nav className="flex-1 py-3 px-2 space-y-1 overflow-y-auto">
        {navItems.map((item) => {
          const isActive = activePanel === item.id;

          return (
            <button
              key={item.id}
              id={`nav-${item.id}`}
              onClick={() => setActivePanel(item.id)}
              title={isSidebarCollapsed ? item.label : undefined}
              className={clsx(
                'w-full flex items-center rounded-lg transition-all duration-150 group relative',
                isSidebarCollapsed ? 'justify-center p-2.5' : 'gap-3 px-3 py-2.5',
                isActive
                  ? 'bg-cyan-500/10 text-cyan-400 hud-border-active'
                  : 'text-slate-500 hover:text-slate-300 hover:bg-surface-2'
              )}
            >
              {/* Active indicator line */}
              {isActive && (
                <div className="absolute left-0 top-1/2 -translate-y-1/2 w-0.5 h-5 bg-cyan-400 rounded-r glow-cyan-sm" />
              )}

              <span className={clsx(isActive && 'text-glow-cyan')}>{item.icon}</span>

              {!isSidebarCollapsed && (
                <span className="text-xs font-medium">{item.label}</span>
              )}

              {/* Phase badge (collapsed) */}
              {!isSidebarCollapsed && item.phase && !isActive && (
                <span className="ml-auto text-[10px] text-muted font-mono opacity-50">
                  P{item.phase}
                </span>
              )}

              {/* Tooltip for collapsed sidebar */}
              {isSidebarCollapsed && (
                <div className="absolute left-full ml-3 px-2 py-1 rounded bg-surface-3 border border-surface-border text-xs text-slate-300 whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none z-50 shadow-xl">
                  {item.label}
                </div>
              )}
            </button>
          );
        })}
      </nav>

      {/* Collapse Toggle */}
      <div className="px-2 pb-3 border-t border-surface-border pt-3">
        <button
          onClick={toggleSidebar}
          className="w-full flex items-center justify-center p-2.5 rounded-lg text-slate-600 hover:text-slate-400 hover:bg-surface-2 transition-smooth"
          title={isSidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          <ChevronLeft
            size={16}
            className={clsx(
              'transition-transform duration-300',
              isSidebarCollapsed && 'rotate-180'
            )}
          />
        </button>
      </div>
    </aside>
  );
}
