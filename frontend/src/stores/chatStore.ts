/**
 * Furi OS — Chat State (Zustand)
 * Manages all conversation state: messages, streaming, session, provider.
 */
import { create } from 'zustand';
import { v4 as uuidv4 } from 'uuid';
import type {
  AgentPlan,
  ChatMessage,
  LLMProviderName,
  PlanStepEventPayload,
  PlanStepStatus,
  StreamChunk,
  TaskEventPayload,
} from '@/types';
import { agentApi, chatApi, tasksApi } from '@/lib/api';
import * as voiceOutput from '@/lib/voiceOutput';
import { rememberSpokenContract } from '@/lib/spokenApproval';

interface ChatState {
  // State
  messages: ChatMessage[];
  sessionId: string;
  isStreaming: boolean;
  streamingMessageId: string | null;
  activeProvider: LLMProviderName;
  error: string | null;
  draftMessage: string;

  // Actions
  sendMessage: (content: string) => Promise<void>;
  appendChunk: (chunk: StreamChunk) => void;
  respondToPlan: (messageId: string, approved: boolean) => Promise<void>;
  respondToChoice: (messageId: string, answer: string) => Promise<void>;
  /** A plan that was answered somewhere OTHER than its card — today, approved
   *  by VOICE (Tier 2 item 8). Keyed by PLAN id, not message id, because the
   *  caller never had a message id: it heard a contract and said yes.
   *  Does exactly what the card path does — patch the card, render the
   *  outcome — so the two cannot drift. Returns the outcome text so a voice
   *  turn can speak it. */
  applyApprovedPlan: (plan: AgentPlan) => string | null;
  /** A reminder (Phase 4, Part 4) fired while this window was open: append
   *  its message live if it belongs to the CURRENT session. A reminder for
   *  a different (or no longer open) session was already persisted by the
   *  backend — it shows up next time that session's history loads. */
  receiveReminderFired: (payload: { session_id?: unknown; body?: unknown; text?: unknown }) => void;
  /** A background task (Phase 4, Part 5) paused or finished: render its
   *  approval card / outcome live if it belongs to the CURRENT session.
   *  Other sessions rely on the toast + the message persisted server-side. */
  receiveTaskEvent: (payload: TaskEventPayload) => void;
  /** Live plan narration (Phase 4, Part 6): one step of an executing plan
   *  changed status — tick the matching row on any card showing that plan.
   *  Best-effort: no card, no problem (the terminal event/response carries
   *  the authoritative final plan). */
  receiveStepEvent: (payload: PlanStepEventPayload) => void;
  /** Mid-plan cancel (Phase 4, Part 6): ask the backend to stop this
   *  message's background task between steps. The cancelled outcome arrives
   *  as a "task" push event, which resolves the card. */
  pauseBackgroundTask: (messageId: string) => Promise<void>;
  cancelBackgroundTask: (messageId: string) => Promise<void>;
  clearConversation: () => void;
  setProvider: (provider: LLMProviderName) => void;
  setError: (error: string | null) => void;
  setDraftMessage: (message: string) => void;
}

export const useChatStore = create<ChatState>((set, get) => {
  /** Append a terminal inline plan's readable outcome (approve/choose
   *  response `outcome_text`) as a normal assistant message. No-op while
   *  the plan is paused or cancelled — the card carries those states. */
  const appendOutcomeText = (plan: AgentPlan) => {
    const text = plan.outcome_text;
    if (!text) return;
    set((state) => ({
      messages: [
        ...state.messages,
        { id: uuidv4(), role: 'assistant', content: text, createdAt: new Date() },
      ],
    }));
  };

  return {
  // ---- Initial State
  messages: [],
  sessionId: uuidv4(),
  isStreaming: false,
  streamingMessageId: null,
  activeProvider: 'gemini',
  error: null,
  draftMessage: '',

  // ---- Actions
  sendMessage: async (content: string) => {
    const { messages, sessionId, activeProvider } = get();

    // Part 4 barge-in: a new message silences the previous reply instantly.
    voiceOutput.stopSpeaking();

    // Add user message immediately
    const userMessage: ChatMessage = {
      id: uuidv4(),
      role: 'user',
      content,
      createdAt: new Date(),
    };

    // Add placeholder assistant message for streaming
    const assistantMessageId = uuidv4();
    const assistantPlaceholder: ChatMessage = {
      id: assistantMessageId,
      role: 'assistant',
      content: '',
      createdAt: new Date(),
      isStreaming: true,
    };

    set({
      messages: [...messages, userMessage, assistantPlaceholder],
      isStreaming: true,
      streamingMessageId: assistantMessageId,
      error: null,
    });

    // Part 4: decide once whether this turn speaks (voice-initiated turns
    // always do; speak_all_responses covers typed ones). The tap below feeds
    // the sentence segmenter — chatStore itself stays thin.
    voiceOutput.beginTurn();

    // Build the message history for the request. Drop empty-content messages:
    // an approval / clarifying-question PlanCard is hosted as an assistant
    // message with no text (the card IS the message), and such an entry carries
    // nothing the LLM needs. Sending it would 422 the whole turn on the backend's
    // content check and lock the session until reload (live bug 2026-07-24).
    const history = [...messages, userMessage]
      .filter((m) => m.content.trim().length > 0)
      .map((m) => ({ role: m.role, content: m.content }));

    await chatApi.streamChat(
      {
        messages: history,
        session_id: sessionId,
        stream: true,
        provider: activeProvider,
      },
      // onChunk
      (chunk) => {
        // Phase 3: the special "plan" message type — attach the agent plan
        // to this message so MessageBubble renders the approval card.
        if (chunk.type === 'plan' && chunk.plan) {
          const plan = chunk.plan;
          // The card carries the interaction (approval buttons OR a
          // clarifying question) — hide the duplicate text bubble for both.
          const interactive =
            plan.requires_approval ||
            plan.status === 'awaiting_choice' ||
            plan.status === 'paused';
          set((state) => ({
            messages: state.messages.map((m) => {
              if (m.id === assistantMessageId) {
                return { ...m, plan, planNeededApproval: interactive };
              }
              // An EARLIER card for this same plan is now stale: answering it
              // by typing consumed it server-side (planner.answer returns the
              // same plan id), so its Approve button would post to a plan that
              // no longer exists. Re-point it at the current state — the
              // buttons go with the status, and a click can never act on a
              // decision the user has already moved past. Same rule
              // receiveTaskEvent applies to a background task's card.
              if (m.plan && m.plan.id === plan.id) {
                return { ...m, plan, planResponding: false };
              }
              return m;
            }),
          }));
          // Read the approval contract ALOUD in its spoken form, instead of
          // the visual one this turn is about to stream as a delta (which is
          // a numbered list of full paths — unholdable by ear). Remembering
          // the hash is what lets a spoken "approve" be BOUND to these exact
          // steps; the server re-derives and refuses a stale one.
          if (plan.requires_approval && plan.spoken_contract && plan.contract_hash) {
            voiceOutput.speakContractInsteadOfTurn(plan.spoken_contract);
            rememberSpokenContract(plan.id, plan.contract_hash);
          }
          return;
        }
        // The ONE voice-output tap (Part 4): every appended delta also feeds
        // the sentence segmenter. Plan chunks returned above never reach it.
        voiceOutput.onDelta(chunk.delta);
        set((state) => ({
          messages: state.messages.map((m) =>
            m.id === assistantMessageId
              ? { ...m, content: m.content + chunk.delta }
              : m
          ),
        }));
      },
      // onDone
      (returnedSessionId) => {
        voiceOutput.endTurn(); // speak the trailing partial sentence
        set((state) => ({
          messages: state.messages.map((m) =>
            m.id === assistantMessageId
              ? { ...m, isStreaming: false }
              : m
          ),
          isStreaming: false,
          streamingMessageId: null,
          sessionId: returnedSessionId || state.sessionId,
        }));
      },
      // onError
      (error) => {
        // Stop queueing further speech; sentences already queued were real,
        // delivered text and finish playing.
        voiceOutput.cancelTurn();
        set((state) => ({
          messages: state.messages.map((m) =>
            m.id === assistantMessageId
              ? {
                  ...m,
                  content: m.content || `Error: ${error}`,
                  isStreaming: false,
                }
              : m
          ),
          isStreaming: false,
          streamingMessageId: null,
          error,
        }));
      }
    );
  },

  appendChunk: (chunk: StreamChunk) => {
    const { streamingMessageId } = get();
    if (!streamingMessageId) return;

    set((state) => ({
      messages: state.messages.map((m) =>
        m.id === streamingMessageId
          ? { ...m, content: m.content + chunk.delta }
          : m
      ),
    }));
  },

  respondToPlan: async (messageId: string, approved: boolean) => {
    const message = get().messages.find((m) => m.id === messageId);
    // The backend consumes the pending plan on the first answer (one answer
    // per plan) — never fire while a response is already in flight. Approval
    // needs the approval gate; Cancel also works on a clarifying question.
    if (!message?.plan || message.planResponding) return;
    const status = message.plan.status;
    if (
      status !== 'awaiting_approval' &&
      // A PAUSED plan takes BOTH: Carry on (approved) re-approves exactly the
      // remaining steps the card is showing, Cancel drops it (2026-08-03).
      status !== 'paused' &&
      !(status === 'awaiting_choice' && !approved)
    )
      return;

    const planId = message.plan.id;
    const patch = (fields: Partial<ChatMessage>) =>
      set((state) => ({
        messages: state.messages.map((m) =>
          m.id === messageId ? { ...m, ...fields } : m
        ),
      }));

    patch({ planResponding: true, planError: null });
    try {
      const updated = await agentApi.approve(planId, approved);
      // The returned plan is final (or re-paused with replanned steps, in
      // which case the card shows the approval buttons again).
      patch({ plan: updated, planResponding: false });
      // A terminal inline plan carries its readable outcome (the same words
      // the typed-chat path streams) — render it as a normal assistant
      // message below the card. The backend already persisted it, so this
      // matches what a reload shows (live bug 2026-07-12: without it, a
      // "…then tell me how many" goal completed silently).
      appendOutcomeText(updated);
    } catch (e) {
      // The approval was consumed (or expired) — don't offer the buttons
      // again; the Activity timeline holds the audit trail.
      patch({
        planResponding: false,
        planError: e instanceof Error ? e.message : 'The plan could not be resumed.',
      });
    }
  },

  applyApprovedPlan: (plan: AgentPlan) => {
    if (!plan?.id) return null;
    // Patch every card tracking this plan — the receiveTaskEvent rule
    // (patch by id, not by position), because a voice approval arrives with
    // no message id and the card may be several turns back.
    set((state) => ({
      messages: state.messages.map((m) =>
        m.plan && m.plan.id === plan.id
          ? { ...m, plan, planResponding: false, planError: null }
          : m
      ),
    }));
    // The SAME append the card path does — without it a plan approved by
    // voice completes in silence and the card goes stale (2026-08-04).
    // ⚠️ Naturally a no-op for a BACKGROUND task: `agent.py` only finalizes
    // inline plans, so a task-owned plan comes back as an executing snapshot
    // with no outcome_text and its real outcome arrives by push. That is what
    // stops this double-speaking, and it is structural, not a check here.
    appendOutcomeText(plan);
    return plan.outcome_text ?? null;
  },

  respondToChoice: async (messageId: string, answer: string) => {
    const message = get().messages.find((m) => m.id === messageId);
    // Same one-answer-per-plan rule as respondToPlan.
    if (!message?.plan || message.planResponding) return;
    // 'paused' too: a correction typed into the paused card is the same
    // gesture as answering a question, and goes down the same endpoint.
    const answerable =
      message.plan.status === 'awaiting_choice' || message.plan.status === 'paused';
    if (!answerable || !answer.trim()) return;

    const planId = message.plan.id;
    const patch = (fields: Partial<ChatMessage>) =>
      set((state) => ({
        messages: state.messages.map((m) =>
          m.id === messageId ? { ...m, ...fields } : m
        ),
      }));

    patch({ planResponding: true, planError: null });
    try {
      // The answer feeds the next planning round; the returned plan is the
      // continuation (often paused again at the approval gate — the same
      // card then shows the concrete steps and the Approve button).
      const updated = await agentApi.choose(planId, answer);
      // Echo the clicked answer as a user bubble — the backend persisted it
      // (clicked options and typed replies are equivalent), so the live view
      // matches what a reload shows.
      set((state) => ({
        messages: [
          ...state.messages,
          { id: uuidv4(), role: 'user', content: answer, createdAt: new Date() },
        ],
      }));
      patch({ plan: updated, planResponding: false });
      // Terminal outcome (count, contents, failure reason…) below the card —
      // see respondToPlan; without this a completed answer arrived nowhere.
      appendOutcomeText(updated);
    } catch (e) {
      patch({
        planResponding: false,
        planError:
          e instanceof Error ? e.message : 'The answer could not be delivered.',
      });
    }
  },

  receiveTaskEvent: (payload) => {
    const { sessionId, messages } = get();
    if (typeof payload.session_id !== 'string' || payload.session_id !== sessionId) return;

    const status = typeof payload.status === 'string' ? payload.status : '';
    const plan =
      payload.plan && typeof payload.plan === 'object'
        ? (payload.plan as AgentPlan)
        : undefined;
    const body = typeof payload.body === 'string' ? payload.body : '';
    const taskId = typeof payload.task_id === 'string' ? payload.task_id : null;

    // 'paused' belongs with the other non-terminal stops (2026-08-03): the card
    // carries the interaction (Continue / Cancel / a typed correction), and the
    // plan is still answerable. Matching on task_id as well as plan id is what
    // lets the ALREADY-VISIBLE executing card become the paused one — a pause
    // usually arrives for a plan the user is watching tick.
    if (
      (status === 'awaiting_approval' ||
        status === 'awaiting_choice' ||
        status === 'paused') &&
      plan
    ) {
      // The card carries the interaction — approving it resumes the
      // background task through the normal respondToPlan / respondToChoice.
      // A re-pause after a replan patches the card already showing this
      // plan (fresh signatures, fresh approval) instead of stacking a new one.
      const existing = messages.find(
        (m) => m.plan?.id === plan.id || (taskId && m.plan?.task_id === taskId)
      );
      if (existing) {
        set((state) => ({
          messages: state.messages.map((m) =>
            m.id === existing.id
              ? {
                  ...m,
                  plan,
                  planNeededApproval: true,
                  planResponding: false,
                  planError: null,
                  planPauseRequested: false,
                }
              : m
          ),
        }));
      } else {
        set({
          messages: [
            ...messages,
            {
              id: uuidv4(),
              role: 'assistant',
              content: '',
              createdAt: new Date(),
              plan,
              planNeededApproval: true,
            },
          ],
        });
      }
      return;
    }

    // Terminal (completed / failed / cancelled): resolve any card still
    // tracking this task, then append the outcome text as its own message
    // (the backend also persisted it — history stays consistent on reload).
    set((state) => {
      const patched = state.messages.map((m) =>
        plan && taskId && m.plan && m.plan.task_id === taskId
          ? { ...m, plan, planCancelRequested: false, planPauseRequested: false }
          : m
      );
      return {
        messages: body
          ? [
              ...patched,
              { id: uuidv4(), role: 'assistant', content: body, createdAt: new Date() },
            ]
          : patched,
      };
    });
  },

  receiveStepEvent: (payload) => {
    const { sessionId } = get();
    if (typeof payload.session_id !== 'string' || payload.session_id !== sessionId) return;
    const planId = typeof payload.plan_id === 'string' ? payload.plan_id : null;
    const stepId = typeof payload.step_id === 'string' ? payload.step_id : null;
    const status = typeof payload.status === 'string' ? payload.status : null;
    if (!planId || !stepId || !status) return;
    if (status !== 'running' && status !== 'completed' && status !== 'failed') return;
    const error = typeof payload.error === 'string' && payload.error ? payload.error : null;

    set((state) => ({
      messages: state.messages.map((m) => {
        if (!m.plan || m.plan.id !== planId) return m;
        return {
          ...m,
          plan: {
            ...m.plan,
            steps: m.plan.steps.map((s) =>
              s.id === stepId
                ? {
                    ...s,
                    status: status as PlanStepStatus,
                    // A failure event carries the error so the row can show
                    // it before the authoritative final plan arrives.
                    result:
                      status === 'failed' && !s.result
                        ? { success: false, output: null, error }
                        : s.result,
                  }
                : s
            ),
          },
        };
      }),
    }));
  },

  pauseBackgroundTask: async (messageId: string) => {
    const message = get().messages.find((m) => m.id === messageId);
    const taskId = message?.plan?.task_id;
    // A cancel already in flight wins — don't ask a dying run to hold.
    if (!taskId || message?.planPauseRequested || message?.planCancelRequested) return;

    const patch = (fields: Partial<ChatMessage>) =>
      set((state) => ({
        messages: state.messages.map((m) =>
          m.id === messageId ? { ...m, ...fields } : m
        ),
      }));

    patch({ planPauseRequested: true, planError: null });
    try {
      const res = await tasksApi.pause(taskId);
      if (!res.accepted) {
        // Nothing live to pause (it just settled, or the backend restarted) —
        // re-enable the button and surface the backend's honest reason.
        patch({
          planPauseRequested: false,
          planError: res.detail || 'The task could not be paused.',
        });
      }
      // accepted: keep "Stopping…" — the paused "task" push event patches the
      // card with the held plan and clears the banner.
    } catch (e) {
      patch({
        planPauseRequested: false,
        planError: e instanceof Error ? e.message : 'The task could not be paused.',
      });
    }
  },

  cancelBackgroundTask: async (messageId: string) => {
    const message = get().messages.find((m) => m.id === messageId);
    const taskId = message?.plan?.task_id;
    if (!taskId || message?.planCancelRequested) return;

    const patch = (fields: Partial<ChatMessage>) =>
      set((state) => ({
        messages: state.messages.map((m) =>
          m.id === messageId ? { ...m, ...fields } : m
        ),
      }));

    patch({ planCancelRequested: true, planError: null });
    try {
      const res = await tasksApi.cancel(taskId);
      if (!res.accepted) {
        // Nothing to cancel (already settled, or the backend restarted) —
        // re-enable the button and surface the backend's honest reason.
        patch({ planCancelRequested: false, planError: res.detail || 'The task could not be cancelled.' });
      }
      // accepted: keep "Cancelling…" — the cancelled "task" push event
      // patches the card with the final plan and clears the banner.
    } catch (e) {
      patch({
        planCancelRequested: false,
        planError: e instanceof Error ? e.message : 'The task could not be cancelled.',
      });
    }
  },

  receiveReminderFired: (payload) => {
    const { sessionId, messages } = get();
    if (typeof payload.session_id !== 'string' || payload.session_id !== sessionId) return;
    const content =
      (typeof payload.body === 'string' && payload.body) ||
      (typeof payload.text === 'string' && payload.text) ||
      'Reminder fired.';
    set({
      messages: [
        ...messages,
        { id: uuidv4(), role: 'assistant', content, createdAt: new Date() },
      ],
    });
  },

  clearConversation: () => {
    voiceOutput.stopSpeaking();
    set({
      messages: [],
      sessionId: uuidv4(),
      isStreaming: false,
      streamingMessageId: null,
      error: null,
    });
  },

  setProvider: (provider: LLMProviderName) => {
    set({ activeProvider: provider });
  },

  setError: (error: string | null) => {
    set({ error });
  },

  setDraftMessage: (message: string) => {
    set({ draftMessage: message });
  },
  };
});
