/**
 * Furi OS — Suggestions Panel (Phase 9, the Initiative Engine)
 *
 * The quiet ambient surface where Furi's proactive suggestions land. Each
 * card leads with the suggestion and a "why it matters" rationale; Accept /
 * Dismiss tune a per-category feedback signal server-side. Accepting a card
 * that carries an action starts an approval-gated background task — its
 * PlanCard appears in chat through the normal push channel. Creation is never
 * manual: suggestions come only from the heartbeat (Settings → Initiative).
 */
import { useEffect } from 'react';
import { clsx } from 'clsx';
import { Check, Lightbulb, RefreshCw, Sparkles, X } from 'lucide-react';
import { useSuggestionsStore } from '@/stores/suggestionsStore';
import type { Suggestion, SuggestionPriority, SuggestionStatus } from '@/types';

const REFRESH_INTERVAL_MS = 15_000;

const PRIORITY_STYLES: Record<SuggestionPriority, { badge: string; label: string }> = {
  high: { badge: 'bg-red-500/10 text-red-400 border-red-500/20', label: 'high' },
  normal: { badge: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/20', label: 'normal' },
  low: { badge: 'bg-surface-2 text-slate-500 border-surface-border', label: 'low' },
};

const STATUS_LABELS: Record<SuggestionStatus, string> = {
  pending: 'pending',
  accepted: 'accepted',
  dismissed: 'dismissed',
  acted: 'acted',
  expired: 'expired',
};

function SuggestionCard({ suggestion }: { suggestion: Suggestion }) {
  const { busyId, accept, dismiss } = useSuggestionsStore();
  const isPending = suggestion.status === 'pending';
  const isBusy = busyId === suggestion.id;
  const priority = PRIORITY_STYLES[suggestion.priority] ?? PRIORITY_STYLES.normal;
  const actionable = suggestion.autonomy === 'ask' || suggestion.autonomy === 'act';

  return (
    <div
      className={clsx(
        'group relative bg-surface-1 border rounded-xl transition-all duration-200 border-surface-border',
        isPending && 'hover:border-cyan-500/20'
      )}
    >
      <div className="flex items-start gap-3 p-3.5">
        <div className="flex-shrink-0 w-7 h-7 rounded-lg border bg-surface-2 border-surface-border flex items-center justify-center mt-0.5 text-cyan-400/70">
          <Lightbulb size={14} />
        </div>

        <div className="flex-1 min-w-0">
          <p className="text-sm font-medium leading-relaxed break-words text-slate-200">
            {suggestion.title}
          </p>
          <p className="text-sm leading-relaxed break-words text-slate-300 mt-0.5">
            {suggestion.body}
          </p>
          {suggestion.rationale && (
            <p className="text-xs leading-relaxed break-words text-muted mt-1.5 italic">
              Why it matters: {suggestion.rationale}
            </p>
          )}
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <span className={clsx('text-[10px] px-1.5 py-0.5 rounded-full border font-mono', priority.badge)}>
              {priority.label}
            </span>
            <span className="text-[10px] text-muted font-mono">{suggestion.category}</span>
            {!isPending && (
              <span className="text-[10px] text-slate-500 font-mono">
                {STATUS_LABELS[suggestion.status]}
              </span>
            )}
          </div>

          {isPending && (
            <div className="flex items-center gap-2 mt-3">
              <button
                onClick={() => void accept(suggestion.id)}
                disabled={isBusy}
                className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium bg-cyan-500/10 text-cyan-300 border border-cyan-500/20 hover:bg-cyan-500/20 transition-colors disabled:opacity-40"
              >
                <Check size={13} />
                {actionable ? 'Do it' : 'Helpful'}
              </button>
              <button
                onClick={() => void dismiss(suggestion.id)}
                disabled={isBusy}
                className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium text-slate-500 hover:text-red-400 hover:bg-red-500/10 transition-colors disabled:opacity-40"
              >
                <X size={13} />
                Dismiss
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export function SuggestionPanel() {
  const { suggestions, isLoading, error, loadSuggestions } = useSuggestionsStore();

  useEffect(() => {
    void loadSuggestions();
    const interval = setInterval(() => void loadSuggestions({ silent: true }), REFRESH_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [loadSuggestions]);

  const pending = suggestions.filter((s) => s.status === 'pending');
  const settled = suggestions.filter((s) => s.status !== 'pending');

  return (
    <div className="flex flex-col h-full bg-surface overflow-hidden">
      {/* ── Header ── */}
      <div className="flex-shrink-0 px-6 py-4 border-b border-surface-border bg-surface-1/50">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-cyan-500/20 to-blue-600/20 border border-cyan-500/20 flex items-center justify-center">
            <Sparkles size={16} className="text-cyan-400" />
          </div>
          <div>
            <h1 className="text-base font-semibold text-slate-200">Suggestions</h1>
            <p className="text-xs text-muted">
              {isLoading
                ? 'Loading...'
                : `${pending.length} pending${settled.length > 0 ? ` · ${settled.length} settled` : ''}`}
            </p>
          </div>
          <div className="flex-1" />
          <button
            onClick={() => void loadSuggestions()}
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

        {isLoading && suggestions.length === 0 && (
          <div className="space-y-2">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-24 bg-surface-1 border border-surface-border rounded-xl animate-pulse" />
            ))}
          </div>
        )}

        {!isLoading && suggestions.length === 0 && !error && (
          <div className="flex flex-col items-center justify-center py-20 text-center gap-4">
            <div className="w-14 h-14 rounded-2xl bg-surface-2 border border-surface-border flex items-center justify-center">
              <Sparkles size={22} className="text-slate-600" />
            </div>
            <div>
              <p className="text-slate-400 text-sm font-medium mb-1">No suggestions yet</p>
              <p className="text-slate-600 text-xs max-w-xs">
                When Furi spots something worth raising, it shows up here. Turn the
                Initiative Engine on in Settings to let it start looking.
              </p>
            </div>
          </div>
        )}

        {pending.length > 0 && (
          <div className="space-y-2">
            {pending.map((s) => (
              <SuggestionCard key={s.id} suggestion={s} />
            ))}
          </div>
        )}

        {settled.length > 0 && (
          <div className="space-y-2 pt-2">
            <p className="text-[10px] uppercase tracking-wide text-slate-600 px-1">Settled</p>
            {settled.map((s) => (
              <SuggestionCard key={s.id} suggestion={s} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
