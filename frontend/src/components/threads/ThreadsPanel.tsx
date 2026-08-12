/**
 * Furi OS — Threads Panel (Phase 11.3 — ongoing-concern tracking)
 * The things you have an open stake in ("the deadline", "waiting to hear back").
 * Furi captures these from conversation and proactively follows up on the open
 * ones via the Initiative Engine. Mark one resolved ("it landed") or dismiss it
 * to stop the nudges.
 */
import { useEffect } from 'react';
import { clsx } from 'clsx';
import { Target, Check, X, CalendarDays } from 'lucide-react';
import { useThreadsStore } from '@/stores/threadsStore';
import type { GoalThread } from '@/types';

const REFRESH_INTERVAL_MS = 15_000;

function statusStyle(status: GoalThread['status']): string {
  if (status === 'resolved') return 'text-emerald-400';
  if (status === 'dropped') return 'text-slate-500';
  return 'text-cyan-400';
}

function ThreadCard({ thread }: { thread: GoalThread }) {
  const { busyId, resolveThread, dismissThread } = useThreadsStore();
  const busy = busyId === thread.id;
  const open = thread.status === 'open';

  return (
    <div className="group relative bg-surface-1 border border-surface-border rounded-xl transition-all duration-200 hover:border-cyan-500/20">
      <div className="flex items-start gap-3 p-3.5">
        <div className={clsx(
          'flex-shrink-0 w-7 h-7 rounded-lg border bg-surface-2 border-surface-border flex items-center justify-center mt-0.5',
          statusStyle(thread.status),
        )}>
          <Target size={14} />
        </div>

        <div className="flex-1 min-w-0">
          <p className={clsx(
            'text-sm font-medium leading-relaxed break-words',
            open ? 'text-slate-200' : 'text-slate-500 line-through',
          )}>
            {thread.title}
          </p>
          {thread.description && (
            <p className="text-xs text-muted mt-1 break-words line-clamp-3">{thread.description}</p>
          )}
          <div className="flex items-center gap-3 mt-1.5 text-[11px]">
            {thread.event_date && (
              <span className="flex items-center gap-1 text-amber-400/70">
                <CalendarDays size={11} /> {thread.event_date}
              </span>
            )}
            {!open && <span className={statusStyle(thread.status)}>{thread.status}</span>}
          </div>
        </div>

        {open && (
          <div className="flex-shrink-0 flex items-center gap-1">
            <button
              onClick={() => void resolveThread(thread.id)}
              disabled={busy}
              className="p-1.5 rounded-lg text-slate-500 hover:text-emerald-400 hover:bg-emerald-500/10 transition-colors disabled:opacity-40"
              title="Mark resolved — it landed"
            >
              <Check size={14} />
            </button>
            <button
              onClick={() => void dismissThread(thread.id)}
              disabled={busy}
              className="p-1.5 rounded-lg text-slate-600 hover:text-red-400 hover:bg-red-500/10 transition-colors disabled:opacity-40"
              title="Dismiss — stop nudging"
            >
              <X size={14} />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export function ThreadsPanel() {
  const { threads, isLoading, error, loadThreads } = useThreadsStore();

  useEffect(() => {
    void loadThreads();
    const interval = setInterval(() => void loadThreads({ silent: true }), REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [loadThreads]);

  const open = threads.filter((t) => t.status === 'open');
  const settled = threads.filter((t) => t.status !== 'open');

  return (
    <div className="flex flex-col h-full bg-surface overflow-hidden">
      <div className="flex-shrink-0 px-6 py-4 border-b border-surface-border bg-surface-1/50">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-cyan-500/20 to-blue-600/20 border border-cyan-500/20 flex items-center justify-center">
            <Target size={16} className="text-cyan-400" />
          </div>
          <div>
            <h1 className="text-base font-semibold text-slate-200">Threads</h1>
            <p className="text-xs text-muted">
              {isLoading ? 'Loading...' : `${open.length} open`}
            </p>
          </div>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-2">
        {error && (
          <div className="p-4 rounded-xl bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
            {error}
          </div>
        )}

        {!isLoading && threads.length === 0 && !error && (
          <div className="flex flex-col items-center justify-center py-20 text-center gap-4">
            <div className="w-14 h-14 rounded-2xl bg-surface-2 border border-surface-border flex items-center justify-center">
              <Target size={22} className="text-slate-600" />
            </div>
            <div>
              <p className="text-slate-400 text-sm font-medium mb-1">No open threads</p>
              <p className="text-slate-600 text-xs max-w-xs">
                When you mention something you have an open stake in &mdash; a deadline you&rsquo;re
                worried about, waiting to hear back &mdash; Furi tracks it here and follows up.
              </p>
            </div>
          </div>
        )}

        {open.length > 0 && (
          <div className="space-y-2">
            {open.map((t) => <ThreadCard key={t.id} thread={t} />)}
          </div>
        )}

        {settled.length > 0 && (
          <div className="pt-4 space-y-2">
            <p className="text-[11px] uppercase tracking-wide text-slate-600 px-1">Settled</p>
            {settled.map((t) => <ThreadCard key={t.id} thread={t} />)}
          </div>
        )}
      </div>
    </div>
  );
}
