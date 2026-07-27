/**
 * Jarvis OS — Agents / Tasks Store (Zustand)
 *
 * The Agents panel's state: the live list of background workers (one per task,
 * each owned by a domain agent) plus per-task live step progress from
 * "plan_step" pushes. Tasks are fetched from GET /api/tasks and reconciled on
 * every "task" push (a pause/finish) with a silent reload — the serializer is
 * the source of truth for domain/status. A short silent poll while the panel is
 * open catches a just-started running task (which does not push until it
 * pauses or finishes).
 */
import { create } from 'zustand';
import type { Task, TaskStatus, TaskEventPayload, PlanStepEventPayload } from '@/types';
import { tasksApi, agentApi } from '@/lib/api';

interface StepProgress {
  index: number;
  count: number;
}

// PlanStatus values the approve/choose endpoints can return that map cleanly
// onto a Task status, for an optimistic patch before the push reconciles.
const PLAN_TO_TASK_STATUS: Record<string, TaskStatus> = {
  executing: 'running',
  awaiting_approval: 'awaiting_approval',
  awaiting_choice: 'awaiting_choice',
  completed: 'completed',
  failed: 'failed',
  cancelled: 'cancelled',
};

interface TasksState {
  tasks: Task[];
  /** Live "step X of N" per task id, from plan_step narration. */
  progress: Record<string, StepProgress>;
  isLoading: boolean;
  error: string | null;
  cancellingId: string | null;
  /** A task whose paused plan is being approved / answered from the panel. */
  respondingId: string | null;
  /** Per-task error from an approve/answer call (plan expired, resume failed). */
  respondErrors: Record<string, string>;

  loadTasks: (opts?: { silent?: boolean }) => Promise<void>;
  cancelTask: (id: string) => Promise<void>;
  /** Approve (or cancel, approved=false) a task paused at the approval gate. */
  respondToTask: (task: Task, approved: boolean) => Promise<void>;
  /** Answer a task paused on a clarifying question (awaiting_choice). */
  answerTask: (task: Task, answer: string) => Promise<void>;
  receiveTaskEvent: (payload: TaskEventPayload) => void;
  receiveStepEvent: (payload: PlanStepEventPayload) => void;
}

export const useTasksStore = create<TasksState>((set, get) => ({
  tasks: [],
  progress: {},
  isLoading: false,
  error: null,
  cancellingId: null,
  respondingId: null,
  respondErrors: {},

  loadTasks: async (opts) => {
    const silent = opts?.silent ?? false;
    if (!silent) set({ isLoading: true, error: null });
    try {
      const tasks = await tasksApi.list();
      set({ tasks, isLoading: false, error: null });
    } catch (e) {
      if (silent) return; // keep the last good list
      set({ error: String(e), isLoading: false });
    }
  },

  cancelTask: async (id: string) => {
    set({ cancellingId: id });
    try {
      await tasksApi.cancel(id);
      // The cancelled outcome arrives by push; reconcile from the server.
      set({ cancellingId: null });
      await get().loadTasks({ silent: true });
    } catch (e) {
      set({ error: String(e), cancellingId: null });
    }
  },

  respondToTask: async (task: Task, approved: boolean) => {
    // The plan id is what /api/agent/approve consumes (pop-once, server-side).
    const planId = task.plan?.id ?? task.plan_id;
    if (!planId) {
      set((s) => ({
        respondErrors: { ...s.respondErrors, [task.id]: 'This task has no plan to approve.' },
      }));
      return;
    }
    set((s) => ({
      respondingId: task.id,
      respondErrors: { ...s.respondErrors, [task.id]: '' },
    }));
    try {
      const plan = await agentApi.approve(planId, approved);
      // Optimistically reflect the returned status so the card clears promptly;
      // the "task" push + poll then reconcile the authoritative row.
      const next = PLAN_TO_TASK_STATUS[plan.status] ?? task.status;
      set((s) => ({
        respondingId: null,
        tasks: s.tasks.map((t) => (t.id === task.id ? { ...t, status: next, plan } : t)),
      }));
      await get().loadTasks({ silent: true });
    } catch (e) {
      set((s) => ({
        respondingId: null,
        respondErrors: { ...s.respondErrors, [task.id]: String(e) },
      }));
    }
  },

  answerTask: async (task: Task, answer: string) => {
    const planId = task.plan?.id ?? task.plan_id;
    if (!planId) {
      set((s) => ({
        respondErrors: { ...s.respondErrors, [task.id]: 'This task has no question to answer.' },
      }));
      return;
    }
    set((s) => ({
      respondingId: task.id,
      respondErrors: { ...s.respondErrors, [task.id]: '' },
    }));
    try {
      const plan = await agentApi.choose(planId, answer);
      const next = PLAN_TO_TASK_STATUS[plan.status] ?? task.status;
      set((s) => ({
        respondingId: null,
        tasks: s.tasks.map((t) => (t.id === task.id ? { ...t, status: next, plan } : t)),
      }));
      await get().loadTasks({ silent: true });
    } catch (e) {
      set((s) => ({
        respondingId: null,
        respondErrors: { ...s.respondErrors, [task.id]: String(e) },
      }));
    }
  },

  receiveTaskEvent: (payload) => {
    // Optimistic status patch for a task already in the list…
    const taskId = typeof payload.task_id === 'string' ? payload.task_id : null;
    const status = typeof payload.status === 'string' ? payload.status : null;
    if (taskId && status) {
      set((state) => ({
        tasks: state.tasks.map((t) =>
          t.id === taskId ? { ...t, status: status as Task['status'] } : t
        ),
      }));
    }
    // …then reconcile from the server (new task rows, domain, message).
    void get().loadTasks({ silent: true });
  },

  receiveStepEvent: (payload) => {
    const taskId = typeof payload.task_id === 'string' ? payload.task_id : null;
    const index = typeof payload.step_index === 'number' ? payload.step_index : null;
    const count = typeof payload.step_count === 'number' ? payload.step_count : null;
    if (!taskId || index === null || count === null) return;
    set((state) => ({ progress: { ...state.progress, [taskId]: { index, count } } }));
  },
}));
