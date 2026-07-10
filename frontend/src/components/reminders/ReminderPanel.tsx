/**
 * Jarvis OS — Reminders Panel (Phase 4, Part 4)
 * Read/cancel surface over the backend's reminder list — creation itself
 * happens through chat ("remind me at 6 to call Jamil"); this panel is
 * where you see what's pending and cancel one before it fires.
 */
import { useEffect } from 'react';
import { clsx } from 'clsx';
import { AlarmClock, Ban, Bell, BellOff, RefreshCw } from 'lucide-react';
import { useRemindersStore } from '@/stores/remindersStore';
import type { Reminder, ReminderStatus } from '@/types';

const REFRESH_INTERVAL_MS = 15_000;

const STATUS_STYLES: Record<ReminderStatus, { badge: string; label: string }> = {
  pending: { badge: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/20', label: 'pending' },
  fired: { badge: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20', label: 'fired' },
  cancelled: { badge: 'bg-surface-2 text-slate-500 border-surface-border', label: 'cancelled' },
};

function dueLabel(iso: string): string {
  const date = new Date(iso);
  const today = new Date();
  const sameDay = date.toDateString() === today.toDateString();
  const time = date.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
  if (sameDay) return `Today at ${time}`;
  const tomorrow = new Date(today);
  tomorrow.setDate(today.getDate() + 1);
  if (date.toDateString() === tomorrow.toDateString()) return `Tomorrow at ${time}`;
  return `${date.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })} at ${time}`;
}

function ReminderCard({ reminder }: { reminder: Reminder }) {
  const { cancellingId, cancelReminder } = useRemindersStore();
  const style = STATUS_STYLES[reminder.status];
  const isPending = reminder.status === 'pending';
  const isCancelling = cancellingId === reminder.id;

  return (
    <div
      className={clsx(
        'group relative bg-surface-1 border rounded-xl transition-all duration-200 border-surface-border',
        isPending && 'hover:border-cyan-500/20'
      )}
    >
      <div className="flex items-start gap-3 p-3.5">
        <div className="flex-shrink-0 w-7 h-7 rounded-lg border bg-surface-2 border-surface-border flex items-center justify-center mt-0.5 text-cyan-400/70">
          <AlarmClock size={14} />
        </div>

        <div className="flex-1 min-w-0">
          <p className="text-sm leading-relaxed break-words text-slate-200">{reminder.text}</p>
          <div className="flex items-center gap-2 mt-1.5 flex-wrap">
            <span className={clsx('text-[10px] px-1.5 py-0.5 rounded-full border font-mono', style.badge)}>
              {style.label}
            </span>
            <span className="text-[10px] text-muted font-mono">{dueLabel(reminder.due_at)}</span>
          </div>
        </div>

        {isPending && (
          <button
            onClick={() => void cancelReminder(reminder.id)}
            disabled={isCancelling}
            className="flex-shrink-0 p-1.5 rounded-lg text-slate-600 hover:text-red-400 hover:bg-red-500/10 transition-colors disabled:opacity-40"
            title="Cancel this reminder"
          >
            <Ban size={14} />
          </button>
        )}
      </div>
    </div>
  );
}

export function ReminderPanel() {
  const { reminders, isLoading, error, loadReminders } = useRemindersStore();

  // Initial load + silent refresh — a reminder set in chat, or one that
  // just fired, should show up here without a manual refresh.
  useEffect(() => {
    void loadReminders();
    const interval = setInterval(() => void loadReminders({ silent: true }), REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [loadReminders]);

  const pending = reminders.filter((r) => r.status === 'pending');
  const settled = reminders.filter((r) => r.status !== 'pending');

  return (
    <div className="flex flex-col h-full bg-surface overflow-hidden">
      {/* ── Header ── */}
      <div className="flex-shrink-0 px-6 py-4 border-b border-surface-border bg-surface-1/50">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-cyan-500/20 to-blue-600/20 border border-cyan-500/20 flex items-center justify-center">
            <Bell size={16} className="text-cyan-400" />
          </div>
          <div>
            <h1 className="text-base font-semibold text-slate-200">Reminders</h1>
            <p className="text-xs text-muted">
              {isLoading
                ? 'Loading...'
                : `${pending.length} pending${settled.length > 0 ? ` · ${settled.length} settled` : ''}`}
            </p>
          </div>
          <div className="flex-1" />
          <button
            onClick={() => void loadReminders()}
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

        {isLoading && reminders.length === 0 && (
          <div className="space-y-2">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-16 bg-surface-1 border border-surface-border rounded-xl animate-pulse" />
            ))}
          </div>
        )}

        {!isLoading && reminders.length === 0 && !error && (
          <div className="flex flex-col items-center justify-center py-20 text-center gap-4">
            <div className="w-14 h-14 rounded-2xl bg-surface-2 border border-surface-border flex items-center justify-center">
              <BellOff size={22} className="text-slate-600" />
            </div>
            <div>
              <p className="text-slate-400 text-sm font-medium mb-1">No reminders yet</p>
              <p className="text-slate-600 text-xs max-w-xs">
                Just say it in chat — "remind me at 6 to call Jamil" — and it shows up here.
              </p>
            </div>
          </div>
        )}

        {pending.length > 0 && (
          <div className="space-y-2">
            {pending.map((r) => (
              <ReminderCard key={r.id} reminder={r} />
            ))}
          </div>
        )}

        {settled.length > 0 && (
          <div className="space-y-2 pt-2">
            <p className="text-[10px] uppercase tracking-wide text-slate-600 px-1">Settled</p>
            {settled.map((r) => (
              <ReminderCard key={r.id} reminder={r} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
