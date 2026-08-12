/**
 * Furi OS — Sidebar Navigation
 * HUD-style vertical icon navigation with active glow effects.
 */
import { clsx } from 'clsx';
import {
  MessageSquare,
  Brain,
  Users,
  Activity,
  Bell,
  Repeat,
  Sparkles,
  Target,
  Bot,
  Terminal,
  Mic,
  Settings,
  ChevronLeft,
  Zap,
} from 'lucide-react';
import { useUIStore } from '@/stores/uiStore';
import { enterVoiceMode } from '@/lib/voiceModeControl';
import type { ActivePanel } from '@/types';

interface NavItem {
  id: ActivePanel;
  icon: React.ReactNode;
  label: string;
  /** Grouping only — the label is never rendered, it just puts a hairline
   *  above the first item of each run so twelve entries read as four groups. */
  group: 'work' | 'knows' | 'does' | 'app';
}

/**
 * ⚠️ The `phase: 2 | 3 | 4 …` field these items used to carry was rendered as a
 * "P2"/"P3" badge next to every label. Those are OUR internal build phases —
 * development scaffolding on the most-visible surface in the product, telling
 * the user nothing and reading as an unfinished prototype. Removed, not
 * relabelled: there is no user-facing question it answers.
 */
const navItems: NavItem[] = [
  { id: 'chat', icon: <MessageSquare size={20} />, label: 'Chat', group: 'work' },
  { id: 'voice', icon: <Mic size={20} />, label: 'Voice', group: 'work' },
  { id: 'memory', icon: <Brain size={20} />, label: 'About Me', group: 'knows' },
  { id: 'contacts', icon: <Users size={20} />, label: 'Contacts', group: 'knows' },
  { id: 'threads', icon: <Target size={20} />, label: 'Threads', group: 'knows' },
  { id: 'agents', icon: <Bot size={20} />, label: 'Agents', group: 'does' },
  { id: 'routines', icon: <Repeat size={20} />, label: 'Routines', group: 'does' },
  { id: 'reminders', icon: <Bell size={20} />, label: 'Reminders', group: 'does' },
  { id: 'initiative', icon: <Sparkles size={20} />, label: 'Suggestions', group: 'does' },
  { id: 'timeline', icon: <Activity size={20} />, label: 'Timeline', group: 'app' },
  { id: 'tools', icon: <Terminal size={20} />, label: 'Tools', group: 'app' },
  { id: 'settings', icon: <Settings size={20} />, label: 'Settings', group: 'app' },
];

export function Sidebar() {
  const { activePanel, setActivePanel, isSidebarCollapsed, toggleSidebar, isVoiceMode } =
    useUIStore();

  // "Voice" is not a panel of its own — voice mode is a surface OVER the chat.
  // (It used to fall through to the "coming soon" placeholder.)
  const openPanel = (id: ActivePanel) => {
    if (id === 'voice') {
      setActivePanel('chat');
      enterVoiceMode();
      return;
    }
    setActivePanel(id);
  };

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
            Furi <span className="text-cyan-400">OS</span>
          </span>
        )}
      </div>

      {/* Navigation Items */}
      <nav className="flex-1 overflow-y-auto px-2 py-3">
        {navItems.map((item, i) => {
          const isActive =
            item.id === 'voice'
              ? isVoiceMode
              : activePanel === item.id && !(item.id === 'chat' && isVoiceMode);
          const startsGroup = i > 0 && navItems[i - 1].group !== item.group;

          return (
            <div key={item.id} className={clsx(startsGroup && 'mt-2 border-t border-surface-border/50 pt-2')}>
              <button
                id={`nav-${item.id}`}
                onClick={() => openPanel(item.id)}
                title={isSidebarCollapsed ? item.label : undefined}
                className={clsx(
                  'group relative flex w-full items-center rounded-lg outline-none transition-colors duration-150',
                  'focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-cyan-400/50',
                  isSidebarCollapsed ? 'justify-center p-2.5' : 'gap-3 px-3 py-2',
                  isActive
                    ? 'bg-cyan-500/[0.08] text-cyan-300'
                    : 'text-slate-500 hover:bg-surface-2 hover:text-slate-300'
                )}
              >
                {/* One active cue, not three. The old row stacked a tinted
                    background, a cyan ring (hud-border-active), a glowing bar
                    and a text-shadow on the icon — four effects saying the same
                    thing, which is most of why the chrome read as noisy. */}
                {isActive && (
                  <span className="absolute left-0 top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-r-full bg-cyan-400" />
                )}

                <span className="flex-shrink-0">{item.icon}</span>

                {!isSidebarCollapsed && (
                  <span className={clsx('truncate text-xs', isActive ? 'font-semibold' : 'font-medium')}>
                    {item.label}
                  </span>
                )}

                {/* Tooltip for collapsed sidebar */}
                {isSidebarCollapsed && (
                  <div className="pointer-events-none absolute left-full z-50 ml-3 whitespace-nowrap rounded border border-surface-border bg-surface-3 px-2 py-1 text-xs text-slate-300 opacity-0 shadow-xl transition-opacity group-hover:opacity-100">
                    {item.label}
                  </div>
                )}
              </button>
            </div>
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
