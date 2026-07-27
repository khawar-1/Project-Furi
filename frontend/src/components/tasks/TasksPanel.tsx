/**
 * Jarvis OS — Agents Panel (boss + domain-specialized agents)
 *
 * Watch and manage the background workers Jarvis's boss dispatched: one per
 * task, each owned by a domain agent (browser / file / email / research /
 * calendar). Shows live status + "step X of N" progress, lets you cancel a
 * running worker, and — for a worker paused at the approval gate or on a
 * clarifying question — renders its PlanCard inline so you can Approve /
 * Cancel / Answer RIGHT HERE. This is the durable escape hatch: the chat card
 * only ever existed from the live push, so a missed push or a session switch
 * used to leave a paused worker unreachable; the panel reads task.plan (served
 * by /api/tasks) and is always reachable from the sidebar.
 */
import { useEffect } from 'react';
import { clsx } from 'clsx';
import {
  Ban,
  Bot,
  CheckCircle2,
  Clock,
  HelpCircle,
  Loader2,
  RefreshCw,
  ShieldAlert,
  XCircle,
} from 'lucide-react';
import { useTasksStore } from '@/stores/tasksStore';
import { PlanCard } from '@/components/chat/PlanCard';
import type { Task, TaskStatus } from '@/types';

const REFRESH_INTERVAL_MS = 5_000; // running tasks change fast

const STATUS: Record<
  TaskStatus,
  { badge: string; label: string; icon: JSX.Element; active: boolean }
> = {
  running: {
    badge: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/20',
    label: 'running',
    icon: <Loader2 size={12} className="animate-spin" />,
    active: true,
  },
  awaiting_approval: {
    badge: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
    label: 'needs your approval',
    icon: <ShieldAlert size={12} />,
    active: true,
  },
  awaiting_choice: {
    badge: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
    label: 'needs your answer',
    icon: <HelpCircle size={12} />,
    active: true,
  },
  completed: {
    badge: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
    label: 'completed',
    icon: <CheckCircle2 size={12} />,
    active: false,
  },
  failed: {
    badge: 'bg-red-500/10 text-red-400 border-red-500/20',
    label: 'failed',
    icon: <XCircle size={12} />,
    active: false,
  },
  cancelled: {
    badge: 'bg-surface-2 text-slate-500 border-surface-border',
    label: 'cancelled',
    icon: <Ban size={12} />,
    active: false,
  },
};

// Per-domain accent for the agent chip, so several running agents read apart.
const AGENT_ACCENT: Record<string, string> = {
  browser: 'text-fuchsia-300 border-fuchsia-500/20 bg-fuchsia-500/10',
  file: 'text-cyan-300 border-cyan-500/20 bg-cyan-500/10',
  email: 'text-blue-300 border-blue-500/20 bg-blue-500/10',
  calendar: 'text-emerald-300 border-emerald-500/20 bg-emerald-500/10',
  research: 'text-violet-300 border-violet-500/20 bg-violet-500/10',
  general: 'text-slate-300 border-surface-border bg-surface-2',
};

function TaskCard({ task }: { task: Task }) {
  const { cancellingId, cancelTask, progress, respondingId, respondErrors, respondToTask, answerTask } =
    useTasksStore();
  const s = STATUS[task.status];
  const accent = AGENT_ACCENT[task.domain ?? 'general'] ?? AGENT_ACCENT.general;
  const isCancelling = cancellingId === task.id;
  const step = task.status === 'running' ? progress[task.id] : undefined;
  const needsYou = task.status === 'awaiting_approval' || task.status === 'awaiting_choice';
  // A paused task carries its full plan (serialized by /api/tasks), so the
  // approval / question card renders RIGHT HERE — reachable regardless of
  // whether the live push was ever seen or which chat session you're in.
  const respondError = respondErrors[task.id] || null;

  return (
    <div className="group relative bg-surface-1 border border-surface-border rounded-xl transition-all duration-200 hover:border-cyan-500/20">
      <div className="flex items-start gap-3 p-3.5">
        <div className="flex-shrink-0 w-7 h-7 rounded-lg border bg-surface-2 border-surface-border flex items-center justify-center mt-0.5 text-cyan-400/70">
          <Bot size={14} />
        </div>

        <div className="flex-1 min-w-0">
          <p className="text-sm leading-relaxed break-words text-slate-200">{task.goal}</p>
          <div className="flex items-center gap-2 mt-1.5 flex-wrap">
            <span
              className={clsx('text-[10px] px-1.5 py-0.5 rounded-full border font-mono', accent)}
            >
              {task.agent}
            </span>
            <span
              className={clsx(
                'text-[10px] px-1.5 py-0.5 rounded-full border font-mono inline-flex items-center gap-1',
                s.badge
              )}
            >
              {s.icon}
              {s.label}
            </span>
            {step && step.count > 0 && (
              <span className="text-[10px] text-muted font-mono">
                step {Math.min(step.index + 1, step.count)} of {step.count}
              </span>
            )}
          </div>
          {needsYou && !task.plan && (
            <p className="mt-2 text-[11px] text-amber-400/80">
              Waiting on you, but its plan is no longer available to show here (it
              may have expired). Try re-running the request.
            </p>
          )}
        </div>

        {task.status === 'running' && (
          <button
            onClick={() => void cancelTask(task.id)}
            disabled={isCancelling}
            className="flex-shrink-0 p-1.5 rounded-lg text-slate-600 hover:text-red-400 hover:bg-red-500/10 transition-colors disabled:opacity-40"
            title="Cancel this task (the running step finishes first)"
          >
            <Ban size={14} />
          </button>
        )}
      </div>

      {/* Inline approval / clarifying-question card for a paused worker. This
          is the escape hatch the "Open Chat →" punt never provided: the plan
          lives in task.plan, so Approve / Cancel / Answer work right here. */}
      {needsYou && task.plan && (
        <div className="px-3.5 pb-3.5 -mt-1">
          <PlanCard
            plan={task.plan}
            responding={respondingId === task.id}
            error={respondError}
            onRespond={(approved) => void respondToTask(task, approved)}
            onAnswer={(answer) => void answerTask(task, answer)}
          />
        </div>
      )}
    </div>
  );
}

export function TasksPanel() {
  const { tasks, isLoading, error, loadTasks } = useTasksStore();

  useEffect(() => {
    void loadTasks();
    const interval = setInterval(() => void loadTasks({ silent: true }), REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [loadTasks]);

  const active = tasks.filter((t) => STATUS[t.status]?.active);
  const settled = tasks.filter((t) => !STATUS[t.status]?.active);

  return (
    <div className="flex flex-col h-full bg-surface overflow-hidden">
      {/* ── Header ── */}
      <div className="flex-shrink-0 px-6 py-4 border-b border-surface-border bg-surface-1/50">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-cyan-500/20 to-blue-600/20 border border-cyan-500/20 flex items-center justify-center">
            <Bot size={16} className="text-cyan-400" />
          </div>
          <div>
            <h1 className="text-base font-semibold text-slate-200">Agents</h1>
            <p className="text-xs text-muted">
              {isLoading && tasks.length === 0
                ? 'Loading...'
                : `${active.length} active${settled.length > 0 ? ` · ${settled.length} finished` : ''}`}
            </p>
          </div>
          <div className="flex-1" />
          <button
            onClick={() => void loadTasks()}
            disabled={isLoading}
            className="p-2 rounded-lg text-slate-500 hover:text-slate-300 hover:bg-surface-2 transition-colors disabled:opacity-40"
            title="Refresh"
          >
            <RefreshCw size={14} className={clsx(isLoading && 'animate-spin')} />
          </button>
        </div>
      </div>

      {/* ── Body ── */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-2">
        {error && (
          <div className="p-4 rounded-xl bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
            {error}
          </div>
        )}

        {isLoading && tasks.length === 0 && (
          <div className="space-y-2">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-16 bg-surface-1 border border-surface-border rounded-xl animate-pulse" />
            ))}
          </div>
        )}

        {!isLoading && tasks.length === 0 && !error && (
          <div className="flex flex-col items-center justify-center py-20 text-center gap-4">
            <div className="w-14 h-14 rounded-2xl bg-surface-2 border border-surface-border flex items-center justify-center">
              <Clock size={22} className="text-slate-600" />
            </div>
            <div>
              <p className="text-slate-400 text-sm font-medium mb-1">No agents working right now</p>
              <p className="text-slate-600 text-xs max-w-xs">
                Give Jarvis a task in chat — "organize my downloads", "apply to the 3 python jobs on
                weworkremotely" — and it hands the work to the right agent here, in the background.
              </p>
            </div>
          </div>
        )}

        {active.length > 0 && (
          <div className="space-y-2">
            {active.map((t) => (
              <TaskCard key={t.id} task={t} />
            ))}
          </div>
        )}

        {settled.length > 0 && (
          <div className="space-y-2 pt-2">
            <p className="text-[10px] uppercase tracking-wide text-slate-600 px-1">Finished</p>
            {settled.map((t) => (
              <TaskCard key={t.id} task={t} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
