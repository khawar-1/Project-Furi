/**
 * Jarvis OS — Multi-Modal Output Router (Phase 12.3)
 *
 * The ONE decision layer for how a proactive push event reaches the user:
 * native toast, spoken voice, or (implicitly) the in-app card. It replaces the
 * two independent gates that used to live in notifications.ts (toast) and
 * voiceAnnounce.ts (speech), reasoning over:
 *
 *     urgency × World-Model presence × window focus × mic state
 *
 * Urgency is derived here from the event type + payload (a pure, testable
 * function) — no backend push changes needed. The two text/audio primitives are
 * reused unchanged: notificationContent() (toast text) and voiceOutput.speakText.
 *
 * In-app store updates (the chatStore/suggestionsStore receivers wired in
 * App.tsx) are NOT an output medium — they always run and are untouched here.
 *
 * GRACEFUL DEFAULT: when the Context Layer is off / the World Model is dark
 * (presence 'unknown', the default), the router reproduces exactly the previous
 * behavior — toast when unfocused & not silent; speak when
 * enabled+output_enabled+speak_proactive & mic idle. Presence-aware
 * refinements only LAYER ON when sensing is active.
 */
import type { PushEvent, UserState } from '@/types';
import { onPush } from '@/lib/push';
import { notificationContent } from '@/lib/notifications';
import { speakText } from '@/lib/voiceOutput';
import { useVoiceStore } from '@/stores/voiceStore';
import { useContextStore } from '@/stores/contextStore';

export type Urgency = 'high' | 'normal' | 'low' | 'ambient';

/** Mirror of the backend context_store.high_load gate: a load read only steers
 *  behavior when it is busy/stressed AND confident enough. Phase 13.2. */
const HIGH_LOAD_MIN_CONFIDENCE = 0.33;
export function isUnderLoad(userState: UserState | null | undefined): boolean {
  if (!userState) return false;
  return (
    (userState.load === 'busy' || userState.load === 'stressed') &&
    (userState.confidence ?? 0) >= HIGH_LOAD_MIN_CONFIDENCE
  );
}

/** Refresh the World-Model presence snapshot at most this often (only while
 *  sensing is enabled) so the router has a current 'active/idle/away' read. */
const PRESENCE_POLL_MS = 60_000;

/** Pure map: an event's urgency from its type + payload. 'ambient' events are
 *  in-app only (no toast, no speech) — silence, like SILENT_TYPES. */
export function classifyUrgency(event: PushEvent): Urgency {
  const type = event.type;
  const payload = (event.payload ?? {}) as Record<string, unknown>;
  if (type === 'connected' || type === 'plan_step') return 'ambient';
  switch (type) {
    case 'reminder':
      return 'high'; // a fired reminder is time-sensitive by definition
    case 'task': {
      // An approval/answer the plan is blocked on is high; an outcome is normal.
      const status = payload.status;
      return status === 'awaiting_approval' || status === 'awaiting_choice'
        ? 'high'
        : 'normal';
    }
    case 'suggestion': {
      const p = payload.priority;
      return p === 'high' ? 'high' : p === 'low' ? 'low' : 'normal';
    }
    case 'briefing':
      return 'normal';
    case 'birthday':
    case 'routine_offer':
      return 'low';
    default:
      return 'normal'; // forward-compatible: an unknown type still notifies
  }
}

/** Decide which output channels a given event fires on, given the live runtime
 *  signals. Exported for reasoning/testing; initOutputRouter applies it. */
export function routeChannels(
  event: PushEvent,
  ctx: {
    focused: boolean;
    presence: string; // active | idle | away | unknown
    underLoad: boolean; // Phase 13.2 — user reads as busy/stressed (confident)
    voice: {
      enabled: boolean;
      output_enabled: boolean;
      speak_proactive: boolean;
      micIdle: boolean;
    };
  }
): { toast: boolean; voice: boolean } {
  const urgency = classifyUrgency(event);
  if (urgency === 'ambient') return { toast: false, voice: false };

  // Under load, defer everything that isn't time-critical: only a HIGH-urgency
  // item (a fired reminder, an approval the plan is blocked on) interrupts.
  if (ctx.underLoad && urgency !== 'high') return { toast: false, voice: false };

  const away = ctx.presence === 'away';

  // Toast: the focused app is its own notification, so suppress toasts while
  // focused — EXCEPT a high-urgency item when the user is away from the screen
  // (a window focused on another monitor still deserves the toast).
  const toast = !ctx.focused || (away && urgency === 'high');

  // Voice: the previous gate (enabled+output+speak_proactive+mic idle), plus one
  // refinement — don't speak a LOW-urgency item while the user is actively
  // focused (the in-app UI already carries it; never interrupt for trivia).
  const voice =
    ctx.voice.enabled &&
    ctx.voice.output_enabled &&
    ctx.voice.speak_proactive &&
    ctx.voice.micIdle &&
    !(urgency === 'low' && ctx.focused && !away);

  return { toast, voice };
}

/** Start the router: the single onPush('*') subscriber for output media.
 *  Returns an unsubscribe function. */
export function initOutputRouter(): () => void {
  // Keep presence current while sensing is on (best-effort, no-op otherwise).
  const poll = window.setInterval(() => {
    if (useContextStore.getState().status?.enabled) {
      void useContextStore.getState().fetchWorld();
    }
  }, PRESENCE_POLL_MS);

  const unsub = onPush('*', (event) => {
    const content = notificationContent(event); // null = SILENT_TYPES / no type
    if (!content) return;

    const v = useVoiceStore.getState();
    const world = useContextStore.getState().world;
    const decision = routeChannels(event, {
      focused: typeof document !== 'undefined' && document.hasFocus(),
      presence: world?.presence ?? 'unknown',
      underLoad: isUnderLoad(world?.user_state),
      voice: {
        enabled: !!v.settings?.enabled,
        output_enabled: !!v.settings?.output_enabled,
        speak_proactive: !!v.settings?.speak_proactive,
        // Never speak into an open mic (recording or transcribing).
        micIdle: v.phase === 'idle',
      },
    });

    if (decision.toast && typeof window.jarvis?.notify === 'function') {
      window.jarvis.notify(content.title, content.body);
    }
    if (decision.voice) {
      // 'Jarvis' is notificationContent's FALLBACK title, not content — saying
      // it before every announcement is noise. A real title leads the sentence.
      speakText(
        content.title === 'Jarvis'
          ? content.body
          : `${content.title}. ${content.body}`
      );
    }
  });

  return () => {
    window.clearInterval(poll);
    unsub();
  };
}
