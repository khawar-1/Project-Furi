/**
 * Jarvis OS — Routines Panel (Phase 6, Part 5)
 * List/run/delete surface over saved routines. Teaching a routine happens
 * through chat ("save this as a routine called clean desktop"); this panel is
 * where you see them, run one on demand, and delete the ones you no longer
 * want. Running re-plans the stored goal fresh — a destructive step still
 * pauses for approval in chat.
 */
import { useEffect } from 'react';
import { clsx } from 'clsx';
import { Play, Trash2, Repeat, Workflow } from 'lucide-react';
import { useRoutinesStore } from '@/stores/routinesStore';
import type { Routine } from '@/types';

const REFRESH_INTERVAL_MS = 15_000;

function RoutineCard({ routine }: { routine: Routine }) {
  const { runningId, deletingId, runRoutine, deleteRoutine } = useRoutinesStore();
  const isRunning = runningId === routine.id;
  const isDeleting = deletingId === routine.id;

  return (
    <div className="group relative bg-surface-1 border border-surface-border rounded-xl transition-all duration-200 hover:border-cyan-500/20">
      <div className="flex items-start gap-3 p-3.5">
        <div className="flex-shrink-0 w-7 h-7 rounded-lg border bg-surface-2 border-surface-border flex items-center justify-center mt-0.5 text-cyan-400/70">
          <Workflow size={14} />
        </div>

        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium leading-relaxed break-words text-slate-200">
            {routine.name}
          </p>
          <p className="text-xs text-muted mt-1 break-words line-clamp-3">{routine.goal_template}</p>
        </div>

        <div className="flex-shrink-0 flex items-center gap-1">
          <button
            onClick={() => void runRoutine(routine.id)}
            disabled={isRunning}
            className="p-1.5 rounded-lg text-slate-500 hover:text-cyan-400 hover:bg-cyan-500/10 transition-colors disabled:opacity-40"
            title="Run this routine now"
          >
            <Play size={14} className={clsx(isRunning && 'animate-pulse')} />
          </button>
          <button
            onClick={() => void deleteRoutine(routine.id)}
            disabled={isDeleting}
            className="p-1.5 rounded-lg text-slate-600 hover:text-red-400 hover:bg-red-500/10 transition-colors disabled:opacity-40"
            title="Delete this routine"
          >
            <Trash2 size={14} />
          </button>
        </div>
      </div>
    </div>
  );
}

export function RoutinesPanel() {
  const { routines, isLoading, error, loadRoutines } = useRoutinesStore();

  useEffect(() => {
    void loadRoutines();
    const interval = setInterval(() => void loadRoutines({ silent: true }), REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [loadRoutines]);

  return (
    <div className="flex flex-col h-full bg-surface overflow-hidden">
      {/* ── Header ── */}
      <div className="flex-shrink-0 px-6 py-4 border-b border-surface-border bg-surface-1/50">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-cyan-500/20 to-blue-600/20 border border-cyan-500/20 flex items-center justify-center">
            <Repeat size={16} className="text-cyan-400" />
          </div>
          <div>
            <h1 className="text-base font-semibold text-slate-200">Routines</h1>
            <p className="text-xs text-muted">
              {isLoading ? 'Loading...' : `${routines.length} saved`}
            </p>
          </div>
        </div>
      </div>

      {/* ── Body ── */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-2">
        {error && (
          <div className="p-4 rounded-xl bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
            {error}
          </div>
        )}

        {isLoading && routines.length === 0 && (
          <div className="space-y-2">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-16 bg-surface-1 border border-surface-border rounded-xl animate-pulse" />
            ))}
          </div>
        )}

        {!isLoading && routines.length === 0 && !error && (
          <div className="flex flex-col items-center justify-center py-20 text-center gap-4">
            <div className="w-14 h-14 rounded-2xl bg-surface-2 border border-surface-border flex items-center justify-center">
              <Workflow size={22} className="text-slate-600" />
            </div>
            <div>
              <p className="text-slate-400 text-sm font-medium mb-1">No routines yet</p>
              <p className="text-slate-600 text-xs max-w-xs">
                Run a task in chat, then say &mdash; &ldquo;save this as a routine called clean
                desktop&rdquo; &mdash; and it shows up here to run any time.
              </p>
            </div>
          </div>
        )}

        {routines.length > 0 && (
          <div className="space-y-2">
            {routines.map((r) => (
              <RoutineCard key={r.id} routine={r} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
