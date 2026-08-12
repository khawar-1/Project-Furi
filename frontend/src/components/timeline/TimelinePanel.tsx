/**
 * Furi OS — Activity Timeline Panel (Phase 3)
 * Chronological audit trail of every tool execution: what ran, with which
 * parameters, whether it succeeded, and how long it took. Read-only view of
 * the backend ActivityLog — the timeline never mutates anything.
 */
import { useEffect, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  ChevronDown,
  FileCode,
  FilePlus,
  FileText,
  FolderOpen,
  MoveRight,
  PenLine,
  RefreshCw,
  Search,
  Terminal,
  Trash2,
  Wrench,
} from 'lucide-react';
import { clsx } from 'clsx';
import { useActivityStore, filterEntries, type ActivityFilter } from '@/stores/activityStore';
import type { ActivityEntry, PermissionLevel } from '@/types';

const REFRESH_INTERVAL_MS = 15_000;

// ── Permission badge styles ──────────────────────────────────────────────────
const PERMISSION_STYLES: Record<PermissionLevel, { badge: string; dot: string; label: string }> = {
  read: {
    badge: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/20',
    dot: 'bg-cyan-400',
    label: 'read',
  },
  write: {
    badge: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
    dot: 'bg-amber-400',
    label: 'write',
  },
  destructive: {
    badge: 'bg-red-500/10 text-red-400 border-red-500/20',
    dot: 'bg-red-400',
    label: 'destructive',
  },
};

// ── Tool icons ────────────────────────────────────────────────────────────────
const TOOL_ICONS: Record<string, React.ReactNode> = {
  search_files: <Search size={14} />,
  read_file: <FileText size={14} />,
  list_directory: <FolderOpen size={14} />,
  move_file: <MoveRight size={14} />,
  rename_file: <PenLine size={14} />,
  delete_file: <Trash2 size={14} />,
  create_file: <FilePlus size={14} />,
  run_command: <Terminal size={14} />,
  execute_script: <FileCode size={14} />,
};

function toolIcon(toolName: string): React.ReactNode {
  return TOOL_ICONS[toolName] ?? <Wrench size={14} />;
}

// ── Date helpers ──────────────────────────────────────────────────────────────
function dayKey(iso: string): string {
  return new Date(iso).toDateString();
}

function dayLabel(iso: string): string {
  const date = new Date(iso);
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (date.toDateString() === today.toDateString()) return 'Today';
  if (date.toDateString() === yesterday.toDateString()) return 'Yesterday';
  return date.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
}

function timeLabel(iso: string): string {
  return new Date(iso).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
}

function durationLabel(ms: number | null): string | null {
  if (ms === null) return null;
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

// ── Single timeline entry ─────────────────────────────────────────────────────
function ActivityCard({ entry }: { entry: ActivityEntry }) {
  const [expanded, setExpanded] = useState(false);
  const style = PERMISSION_STYLES[entry.permission_level] ?? PERMISSION_STYLES.read;
  const duration = durationLabel(entry.duration_ms);
  const hasDetail = entry.parameters !== null || entry.result_summary !== null;

  return (
    <div
      className={clsx(
        'group relative bg-surface-1 border rounded-xl transition-all duration-200',
        entry.success
          ? 'border-surface-border hover:border-cyan-500/20'
          : 'border-red-500/20 hover:border-red-500/40'
      )}
    >
      {/* Left accent: permission colour, red when failed */}
      <div
        className={clsx(
          'absolute left-0 top-3 bottom-3 w-0.5 rounded-r',
          entry.success ? style.dot : 'bg-red-400'
        )}
      />

      <button
        onClick={() => hasDetail && setExpanded((v) => !v)}
        className={clsx(
          'w-full flex items-start gap-3 p-3.5 text-left',
          hasDetail && 'cursor-pointer'
        )}
      >
        {/* Tool icon */}
        <div
          className={clsx(
            'flex-shrink-0 w-7 h-7 rounded-lg border flex items-center justify-center mt-0.5',
            entry.success
              ? clsx('bg-surface-2 border-surface-border', style.badge.split(' ')[1])
              : 'bg-red-500/10 border-red-500/20 text-red-400'
          )}
        >
          {entry.success ? toolIcon(entry.tool_name) : <AlertTriangle size={14} />}
        </div>

        <div className="flex-1 min-w-0">
          <p className={clsx('text-sm leading-relaxed break-words', entry.success ? 'text-slate-200' : 'text-red-300')}>
            {entry.action}
          </p>
          <div className="flex items-center gap-2 mt-1.5 flex-wrap">
            <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-surface-2 border border-surface-border text-slate-400 font-mono">
              {entry.tool_name}
            </span>
            <span className={clsx('text-[10px] px-1.5 py-0.5 rounded-full border font-mono', style.badge)}>
              {style.label}
            </span>
            {!entry.success && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-red-500/10 text-red-400 border border-red-500/20 font-mono">
                failed
              </span>
            )}
            {duration && <span className="text-[10px] text-muted font-mono">{duration}</span>}
            <span className="text-[10px] text-muted">{timeLabel(entry.created_at)}</span>
          </div>
        </div>

        {hasDetail && (
          <ChevronDown
            size={14}
            className={clsx(
              'flex-shrink-0 mt-1 text-slate-600 group-hover:text-slate-400 transition-transform duration-200',
              expanded && 'rotate-180'
            )}
          />
        )}
      </button>

      {/* Expanded detail: parameters + result */}
      {expanded && hasDetail && (
        <div className="px-3.5 pb-3.5 pl-[3.25rem] space-y-2 animate-fade-in">
          {entry.parameters !== null && (
            <div>
              <p className="text-[10px] uppercase tracking-wide text-slate-600 mb-1 font-mono">Parameters</p>
              <pre className="text-[11px] text-slate-400 bg-surface-2 border border-surface-border rounded-lg p-2.5 overflow-x-auto font-mono whitespace-pre-wrap break-all">
                {typeof entry.parameters === 'string'
                  ? entry.parameters
                  : JSON.stringify(entry.parameters, null, 2)}
              </pre>
            </div>
          )}
          {entry.result_summary && (
            <div>
              <p className="text-[10px] uppercase tracking-wide text-slate-600 mb-1 font-mono">
                {entry.success ? 'Result' : 'Error'}
              </p>
              <pre
                className={clsx(
                  'text-[11px] rounded-lg p-2.5 overflow-x-auto font-mono whitespace-pre-wrap break-all border',
                  entry.success
                    ? 'text-slate-400 bg-surface-2 border-surface-border'
                    : 'text-red-300 bg-red-500/5 border-red-500/20'
                )}
              >
                {entry.result_summary}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Filter chips ──────────────────────────────────────────────────────────────
const FILTERS: Array<{ id: ActivityFilter; label: string }> = [
  { id: 'all', label: 'All' },
  { id: 'read', label: 'Read' },
  { id: 'write', label: 'Write' },
  { id: 'destructive', label: 'Destructive' },
  { id: 'failed', label: 'Failed' },
];

// ── Main component ────────────────────────────────────────────────────────────
export function TimelinePanel() {
  const {
    entries, isLoading, error, filter, sessionOnly,
    loadActivity, setFilter, setSessionOnly,
  } = useActivityStore();

  // Initial load + silent refresh while the panel is open (the agent writes
  // new entries in the background as tasks execute).
  useEffect(() => {
    void loadActivity();
    const interval = setInterval(() => void loadActivity({ silent: true }), REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [loadActivity]);

  const visible = filterEntries(entries, filter);
  const failedCount = entries.filter((e) => !e.success).length;

  // Group by calendar day, newest day first (backend already sorts entries)
  const groups: Array<{ label: string; items: ActivityEntry[] }> = [];
  for (const entry of visible) {
    const label = dayLabel(entry.created_at);
    const last = groups[groups.length - 1];
    if (last && dayKey(entry.created_at) === dayKey(last.items[0].created_at)) {
      last.items.push(entry);
    } else {
      groups.push({ label, items: [entry] });
    }
  }

  return (
    <div className="flex flex-col h-full bg-surface overflow-hidden">
      {/* ── Header ── */}
      <div className="flex-shrink-0 px-6 py-4 border-b border-surface-border bg-surface-1/50">
        <div className="flex items-center gap-3 mb-4">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-cyan-500/20 to-blue-600/20 border border-cyan-500/20 flex items-center justify-center">
            <Activity size={16} className="text-cyan-400" />
          </div>
          <div>
            <h1 className="text-base font-semibold text-slate-200">Activity Timeline</h1>
            <p className="text-xs text-muted">
              {isLoading
                ? 'Loading...'
                : `${entries.length} action${entries.length !== 1 ? 's' : ''}${failedCount > 0 ? ` · ${failedCount} failed` : ''}`}
            </p>
          </div>
          <div className="flex-1" />
          <button
            onClick={() => void loadActivity()}
            disabled={isLoading}
            className="p-2 rounded-lg text-slate-500 hover:text-slate-300 hover:bg-surface-2 transition-colors disabled:opacity-40"
            title="Refresh"
          >
            <RefreshCw size={14} className={clsx(isLoading && 'animate-spin')} />
          </button>
        </div>

        {/* Filter chips + session toggle */}
        <div className="flex items-center gap-1.5 flex-wrap">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              onClick={() => setFilter(f.id)}
              className={clsx(
                'text-[11px] px-2.5 py-1 rounded-full border transition-colors',
                filter === f.id
                  ? 'bg-cyan-500/15 text-cyan-400 border-cyan-500/30'
                  : 'bg-surface-2 text-slate-500 border-surface-border hover:text-slate-300'
              )}
            >
              {f.label}
            </button>
          ))}
          <div className="flex-1" />
          <button
            onClick={() => setSessionOnly(!sessionOnly)}
            className={clsx(
              'text-[11px] px-2.5 py-1 rounded-full border transition-colors',
              sessionOnly
                ? 'bg-cyan-500/15 text-cyan-400 border-cyan-500/30'
                : 'bg-surface-2 text-slate-500 border-surface-border hover:text-slate-300'
            )}
            title="Show only actions from the current chat session"
          >
            This chat
          </button>
        </div>
      </div>

      {/* ── Body ── */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-2">
        {/* Error */}
        {error && (
          <div className="p-4 rounded-xl bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
            {error}
          </div>
        )}

        {/* Loading skeleton */}
        {isLoading && entries.length === 0 && (
          <div className="space-y-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="h-16 bg-surface-1 border border-surface-border rounded-xl animate-pulse" />
            ))}
          </div>
        )}

        {/* Empty state */}
        {!isLoading && visible.length === 0 && !error && (
          <div className="flex flex-col items-center justify-center py-20 text-center gap-4">
            <div className="w-14 h-14 rounded-2xl bg-surface-2 border border-surface-border flex items-center justify-center">
              <Activity size={22} className="text-slate-600" />
            </div>
            <div>
              <p className="text-slate-400 text-sm font-medium mb-1">
                {entries.length === 0 ? 'No activity yet' : 'Nothing matches this filter'}
              </p>
              <p className="text-slate-600 text-xs max-w-xs">
                {entries.length === 0
                  ? 'When Furi runs tasks for you — searching, creating, or organizing files — every action shows up here.'
                  : 'Try a different filter, or switch off "This chat".'}
              </p>
            </div>
          </div>
        )}

        {/* Timeline, grouped by day */}
        {groups.map((group) => (
          <div key={group.label} className="space-y-2">
            <div className="flex items-center gap-3 pt-2">
              <span className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider">
                {group.label}
              </span>
              <div className="flex-1 h-px bg-surface-border" />
            </div>
            {group.items.map((entry) => (
              <ActivityCard key={entry.id} entry={entry} />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
