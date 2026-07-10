/**
 * Jarvis OS — Plan Card (Phase 3, Part 8)
 * Renders an agent plan inside the chat: every step with its permission
 * badge, DESTRUCTIVE steps flagged red with a warning, and Approve / Cancel
 * buttons for plans paused at the approval gate. After the user answers,
 * the card re-renders from the final plan returned by POST /api/agent/approve
 * — per-step outcomes and results included.
 */
import { useState } from 'react';
import { clsx } from 'clsx';
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  Circle,
  HelpCircle,
  ListChecks,
  Loader2,
  MinusCircle,
  Send,
  X,
  XCircle,
} from 'lucide-react';
import type { AgentPlan, PermissionLevel, PlanStatus, PlanStep } from '@/types';

// Same colour language as the Activity Timeline: read=cyan, write=amber,
// destructive=red.
const PERMISSION_BADGES: Record<PermissionLevel, string> = {
  read: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/20',
  write: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
  destructive: 'bg-red-500/10 text-red-400 border-red-500/20',
};

const STATUS_META: Record<PlanStatus, { label: string; badge: string; border: string }> = {
  executing: {
    label: 'executing',
    badge: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/20',
    border: 'border-surface-border',
  },
  awaiting_approval: {
    label: 'needs your approval',
    badge: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
    border: 'border-amber-500/30',
  },
  awaiting_choice: {
    label: 'needs your answer',
    badge: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/20',
    border: 'border-cyan-500/30',
  },
  completed: {
    label: 'completed',
    badge: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
    border: 'border-emerald-500/20',
  },
  failed: {
    label: 'failed',
    badge: 'bg-red-500/10 text-red-400 border-red-500/20',
    border: 'border-red-500/30',
  },
  cancelled: {
    label: 'cancelled',
    badge: 'bg-surface-2 text-slate-400 border-surface-border',
    border: 'border-surface-border',
  },
};

function StepStatusIcon({ step }: { step: PlanStep }) {
  switch (step.status) {
    case 'running':
      // Live narration (Phase 4, Part 6): this step's tool call is in flight.
      return <Loader2 size={14} className="animate-spin text-cyan-400" />;
    case 'completed':
      return <CheckCircle2 size={14} className="text-emerald-400" />;
    case 'failed':
      return <XCircle size={14} className="text-red-400" />;
    case 'skipped':
      return <MinusCircle size={14} className="text-slate-600" />;
    default:
      return (
        <Circle
          size={14}
          className={step.permission_level === 'destructive' ? 'text-red-400' : 'text-slate-500'}
        />
      );
  }
}

/** Expandable detail text for a step: its error, its result output, or —
 *  for a step that has not run yet — the exact parameters it will run with,
 *  so the user can see precisely what they are approving. */
function stepDetail(step: PlanStep): string | null {
  if (!step.result) {
    const params = step.parameters ?? {};
    if (Object.keys(params).length === 0) return null;
    return `Will run with:\n${JSON.stringify(params, null, 2)}`;
  }
  if (step.result.error) return step.result.error;
  if (step.result.output === null || step.result.output === undefined) return null;
  const text =
    typeof step.result.output === 'string'
      ? step.result.output
      : JSON.stringify(step.result.output, null, 2);
  return text.trim() ? text : null;
}

/** One-line summary of what a step actually does — the backend's
 *  code-derived action_detail when present, else the main parameter
 *  (command / script / path), falling back to the raw parameter JSON. */
function paramSummary(step: PlanStep): string {
  if (step.action_detail) return step.action_detail;
  const p = (step.parameters ?? {}) as Record<string, unknown>;
  const main = p.command ?? p.script_path ?? p.path ?? p.source;
  if (typeof main === 'string' && main.trim()) return main;
  const json = JSON.stringify(p);
  return json && json !== '{}' ? json : '(no parameters)';
}

function StepRow({ step, index }: { step: PlanStep; index: number }) {
  const [expanded, setExpanded] = useState(false);
  const isDestructive = step.permission_level === 'destructive';
  const detail = stepDetail(step);
  const failed = step.status === 'failed';

  return (
    <div
      className={clsx(
        'rounded-lg border',
        failed
          ? 'bg-red-500/5 border-red-500/20'
          : isDestructive && step.status === 'pending'
            ? 'bg-red-500/5 border-red-500/30'
            : 'bg-surface-2/50 border-surface-border'
      )}
    >
      <button
        onClick={() => detail && setExpanded((v) => !v)}
        className={clsx(
          'w-full flex items-start gap-2.5 px-3 py-2 text-left',
          detail && 'cursor-pointer'
        )}
      >
        <span className="flex-shrink-0 mt-0.5">
          <StepStatusIcon step={step} />
        </span>
        <span className="flex-shrink-0 mt-px text-[11px] font-mono text-slate-600 w-4">
          {index}.
        </span>
        <div className="flex-1 min-w-0">
          <p
            className={clsx(
              'text-[13px] leading-relaxed break-words',
              step.status === 'skipped' ? 'text-slate-500 line-through' : 'text-slate-200'
            )}
          >
            {step.description}
          </p>
          {/* Verbatim, code-derived action — always visible for write and
              destructive steps, so approval never rests on the LLM's prose. */}
          {step.action_detail && step.permission_level !== 'read' && (
            <pre
              className={clsx(
                'mt-1 text-[11px] font-mono whitespace-pre-wrap break-all rounded-md px-2 py-1 border',
                isDestructive
                  ? 'text-red-200 bg-red-500/5 border-red-500/20'
                  : 'text-amber-200/90 bg-amber-500/5 border-amber-500/20'
              )}
            >
              {step.action_detail}
            </pre>
          )}
          <div className="flex items-center gap-1.5 mt-1 flex-wrap">
            <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-surface-2 border border-surface-border text-slate-400 font-mono">
              {step.tool}
            </span>
            <span
              title={isDestructive ? paramSummary(step) : undefined}
              className={clsx(
                'text-[10px] px-1.5 py-0.5 rounded-full border font-mono',
                isDestructive && 'cursor-help',
                PERMISSION_BADGES[step.permission_level] ?? PERMISSION_BADGES.read
              )}
            >
              {isDestructive && <AlertTriangle size={9} className="inline mr-1 -mt-px" />}
              {step.permission_level}
            </span>
            {failed && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-red-500/10 text-red-400 border border-red-500/20 font-mono">
                failed
              </span>
            )}
          </div>
        </div>
        {detail && (
          <ChevronDown
            size={13}
            className={clsx(
              'flex-shrink-0 mt-1 text-slate-600 transition-transform duration-200',
              expanded && 'rotate-180'
            )}
          />
        )}
      </button>

      {expanded && detail && (
        <div className="px-3 pb-2.5 pl-[3.35rem] animate-fade-in">
          <pre
            className={clsx(
              'text-[11px] rounded-lg p-2.5 overflow-x-auto font-mono whitespace-pre-wrap break-all border max-h-56 overflow-y-auto',
              failed
                ? 'text-red-300 bg-red-500/5 border-red-500/20'
                : 'text-slate-400 bg-surface-1 border-surface-border'
            )}
          >
            {detail}
          </pre>
        </div>
      )}
    </div>
  );
}

interface PlanCardProps {
  plan: AgentPlan;
  /** An approve/cancel/answer call is in flight. */
  responding: boolean;
  /** Error from the approve/cancel/answer call (plan expired, resume failed). */
  error: string | null;
  onRespond: (approved: boolean) => void;
  /** Answer the plan's clarifying question (status awaiting_choice). */
  onAnswer?: (answer: string) => void;
  /** Mid-plan cancel was requested and is pending (Phase 4, Part 6). */
  cancelRequested?: boolean;
  /** Request a cooperative mid-plan cancel of the background task. */
  onCancelTask?: () => void;
}

export function PlanCard({
  plan,
  responding,
  error,
  onRespond,
  onAnswer,
  cancelRequested,
  onCancelTask,
}: PlanCardProps) {
  const [choice, setChoice] = useState<'approve' | 'cancel' | null>(null);
  const [answerText, setAnswerText] = useState('');
  const status = STATUS_META[plan.status] ?? STATUS_META.executing;
  const awaiting = plan.status === 'awaiting_approval' && !error;
  const asking = plan.status === 'awaiting_choice' && !error && plan.question != null;
  const destructiveCount = plan.steps.filter(
    (s) => s.status === 'pending' && s.permission_level === 'destructive'
  ).length;
  const completedCount = plan.steps.filter((s) => s.status === 'completed').length;

  const respond = (approved: boolean) => {
    setChoice(approved ? 'approve' : 'cancel');
    onRespond(approved);
  };

  return (
    <div
      className={clsx(
        'w-full rounded-2xl border bg-surface-1 overflow-hidden animate-fade-in',
        status.border
      )}
    >
      {/* Header */}
      <div className="flex items-center gap-2.5 px-4 pt-3.5 pb-3">
        <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-cyan-500/20 to-blue-600/20 border border-cyan-500/20 flex items-center justify-center flex-shrink-0">
          <ListChecks size={14} className="text-cyan-400" />
        </div>
        <span className="text-sm font-semibold text-slate-200">Task plan</span>
        <span
          className={clsx(
            'text-[10px] px-1.5 py-0.5 rounded-full border font-mono',
            status.badge
          )}
        >
          {status.label}
        </span>
      </div>

      {/* Steps (a plan paused on a question may not have any yet) */}
      {plan.steps.length > 0 && (
        <div className="px-4 pb-3 space-y-1.5">
          {plan.steps.map((step, i) => (
            <StepRow key={step.id} step={step} index={i + 1} />
          ))}
        </div>
      )}

      {/* Background execution (Phase 4, Part 5): the plan left the chat
          turn — approval was accepted and the steps are running detached.
          Steps tick live from "plan_step" push events (Part 6), and Cancel
          requests a cooperative stop: the step currently running finishes
          (never killed mid-write), then the plan settles as cancelled. */}
      {!error && plan.task_id && plan.status === 'executing' && (
        <div className="mx-4 mb-4 flex items-center gap-2 px-3 py-2 rounded-lg bg-cyan-500/10 border border-cyan-500/20 text-cyan-200 text-xs">
          <Loader2 size={14} className="animate-spin text-cyan-400 flex-shrink-0" />
          <span className="flex-1">
            {cancelRequested
              ? 'Cancelling — the step currently running will finish first; nothing further will run.'
              : 'Running in the background — Jarvis will notify you when it finishes.'}
          </span>
          {!cancelRequested && onCancelTask && (
            <button
              onClick={onCancelTask}
              className="flex items-center gap-1 flex-shrink-0 px-2.5 py-1 rounded-lg border border-surface-border bg-surface-2 text-[11px] font-semibold text-slate-300 hover:text-slate-100 hover:border-slate-500/40 transition-colors"
            >
              <X size={12} />
              Cancel
            </button>
          )}
        </div>
      )}

      {/* Outcome banners */}
      {error && (
        <div className="mx-4 mb-4 flex items-start gap-2 px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/20 text-red-300 text-xs">
          <AlertTriangle size={14} className="flex-shrink-0 mt-0.5 text-red-400" />
          <span className="break-words">{error}</span>
        </div>
      )}
      {!error && plan.status === 'completed' && (
        <div className="mx-4 mb-4 px-3 py-2 rounded-lg bg-emerald-500/10 border border-emerald-500/20 text-emerald-300 text-xs">
          Completed — {completedCount} step{completedCount !== 1 ? 's' : ''} ran.
          {plan.message ? ` ${plan.message}` : ''}
        </div>
      )}
      {!error && plan.status === 'failed' && (
        <div className="mx-4 mb-4 px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/20 text-red-300 text-xs">
          {plan.message || 'The plan could not be completed.'}
          {completedCount > 0 &&
            ` (${completedCount} step${completedCount !== 1 ? 's' : ''} completed before the failure — see the Activity timeline.)`}
        </div>
      )}
      {!error && plan.status === 'cancelled' && (
        <div className="mx-4 mb-4 px-3 py-2 rounded-lg bg-surface-2 border border-surface-border text-slate-400 text-xs">
          Cancelled — nothing was changed.
        </div>
      )}

      {/* Clarifying question — clickable options, chat typing also works.
          Answering executes nothing; any resulting write step still pauses
          at the approval gate below with fresh signatures. */}
      {asking && plan.question && (
        <div className="px-4 pb-4 space-y-2.5">
          <div className="flex items-start gap-2 px-3 py-2 rounded-lg bg-cyan-500/10 border border-cyan-500/30 text-cyan-100 text-xs">
            <HelpCircle size={14} className="flex-shrink-0 mt-0.5 text-cyan-400" />
            <span className="break-words whitespace-pre-wrap">{plan.question.text}</span>
          </div>
          {responding ? (
            <div className="flex items-center gap-2 text-xs text-slate-400 py-1">
              <Loader2 size={14} className="animate-spin text-cyan-400" />
              {choice === 'cancel' ? 'Cancelling…' : 'Continuing with your answer…'}
            </div>
          ) : (
            <>
              {plan.question.options.length > 0 && (
                <div className="flex flex-col gap-1.5">
                  {plan.question.options.map((option, i) => (
                    <button
                      key={i}
                      onClick={() => onAnswer?.(option)}
                      className="text-left px-3 py-2 rounded-lg border border-surface-border bg-surface-2 text-xs font-mono text-slate-200 hover:border-cyan-500/40 hover:bg-cyan-500/10 transition-colors break-all"
                    >
                      {option}
                    </button>
                  ))}
                </div>
              )}
              {/* Inline free-text answer — an options-free question must never
                  leave the user with only Cancel (the chat box works too, but
                  the card carries the interaction). */}
              <form
                className="flex items-center gap-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  const text = answerText.trim();
                  if (text) onAnswer?.(text);
                }}
              >
                <input
                  type="text"
                  value={answerText}
                  onChange={(e) => setAnswerText(e.target.value)}
                  placeholder="Type your answer…"
                  className="flex-1 min-w-0 px-3 py-1.5 rounded-lg border border-surface-border bg-surface-2 text-xs text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-cyan-500/40"
                />
                <button
                  type="submit"
                  disabled={!answerText.trim()}
                  className="flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg border border-cyan-500/40 bg-cyan-500/15 text-xs font-semibold text-cyan-300 hover:bg-cyan-500/25 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  <Send size={13} />
                  Answer
                </button>
              </form>
              <div className="flex items-center gap-2 flex-wrap">
                <button
                  onClick={() => respond(false)}
                  className="flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg border border-surface-border bg-surface-2 text-xs font-semibold text-slate-300 hover:text-slate-100 hover:border-slate-500/40 transition-colors"
                >
                  <X size={13} />
                  Cancel
                </button>
                <span className="text-[10px] text-muted">
                  Pick an option or answer in your own words. Nothing runs
                  until you approve the resulting steps.
                </span>
              </div>
            </>
          )}
        </div>
      )}

      {/* Approval gate */}
      {awaiting && (
        <div className="px-4 pb-4 space-y-2.5">
          {destructiveCount > 0 && (
            <div className="flex items-start gap-2 px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/30 text-red-300 text-xs">
              <AlertTriangle size={14} className="flex-shrink-0 mt-0.5 text-red-400" />
              <span>
                This plan includes {destructiveCount} destructive step
                {destructiveCount !== 1 ? 's' : ''} — the exact command is
                shown on each red step above. Files Jarvis deletes go to a
                recoverable trash (~/.jarvis/trash); shell commands may make
                changes Jarvis cannot undo.
              </span>
            </div>
          )}
          {responding ? (
            <div className="flex items-center gap-2 text-xs text-slate-400 py-1">
              <Loader2 size={14} className="animate-spin text-cyan-400" />
              {choice === 'cancel' ? 'Cancelling…' : 'Executing approved steps…'}
            </div>
          ) : (
            <div className="flex items-center gap-2 flex-wrap">
              <button
                onClick={() => respond(true)}
                className={clsx(
                  'flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg border text-xs font-semibold transition-colors',
                  destructiveCount > 0
                    ? 'bg-red-500/15 text-red-300 border-red-500/40 hover:bg-red-500/25'
                    : 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40 hover:bg-emerald-500/25'
                )}
              >
                <Check size={13} />
                Approve &amp; run
              </button>
              <button
                onClick={() => respond(false)}
                className="flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg border border-surface-border bg-surface-2 text-xs font-semibold text-slate-300 hover:text-slate-100 hover:border-slate-500/40 transition-colors"
              >
                <X size={13} />
                Cancel
              </button>
              <span className="text-[10px] text-muted">
                Nothing runs until you approve.
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
