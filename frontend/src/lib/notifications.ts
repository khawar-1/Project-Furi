/**
 * Jarvis OS — Native Notification Bridge (Phase 4, Part 3)
 *
 * Turns server-pushed events into native OS notifications through the one
 * minimal bridge method window.jarvis.notify(title, body). The renderer only
 * ever hands over two plain strings — the toast itself is created in the
 * Electron main process, so the renderer still never touches Node. In a plain
 * browser (no Electron preload) this whole module is a no-op.
 *
 * Rules:
 * - The 'connected' frame is the channel handshake, not an event — silent.
 * - A focused window is its own notification: toasts only fire while the app
 *   is in the tray or behind other windows (document.hasFocus() === false).
 * - Payloads are DATA: only their designated text fields (title / body /
 *   message / text) are surfaced, with deterministic fallbacks — unknown
 *   event types still notify ("forward compatible", like the dispatcher).
 */
import type { PushEvent } from '@/types';
import { onPush } from '@/lib/push';

/** Event types that must never raise a notification: the channel handshake,
 *  and per-step plan narration (Phase 4, Part 6) — a plan can emit dozens of
 *  step ticks; the plan-level "task" events are the ones worth a toast. */
const SILENT_TYPES = new Set(['connected', 'plan_step']);

function asText(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

/** Phase 9 — intelligent notification framing for proactive suggestions:
 *  reasoned and prioritized, leading with the "why it matters" rationale. A
 *  high-priority suggestion is marked so it reads as more urgent at a glance. */
function suggestionContent(
  payload: Record<string, unknown>
): { title: string; body: string } | null {
  const heading = asText(payload.title);
  const body = asText(payload.body);
  if (!heading || !body) return null;
  const rationale = asText(payload.rationale);
  const priority = asText(payload.priority);
  const title = priority === 'high' ? `⚡ ${heading}` : `Jarvis · ${heading}`;
  // Lead with what it is; append why it matters when we have it.
  const fullBody = rationale ? `${body} — ${rationale}` : body;
  return { title, body: fullBody };
}

/** The title/body a push event should notify with; null = stay silent. */
export function notificationContent(
  event: PushEvent
): { title: string; body: string } | null {
  if (!event.type || SILENT_TYPES.has(event.type)) return null;
  const payload = event.payload ?? {};
  // Per-type intelligent framing (Phase 9). Other types fall through to the
  // generic title/body/message/text extraction (forward-compatible).
  if (event.type === 'suggestion') return suggestionContent(payload);
  const title = asText(payload.title) ?? 'Jarvis';
  const body =
    asText(payload.body) ??
    asText(payload.message) ??
    asText(payload.text) ??
    `New event: ${event.type}`;
  return { title, body };
}

/** Start forwarding push events to native notifications.
 *  Returns an unsubscribe function. */
export function initNotifications(): () => void {
  return onPush('*', (event) => {
    // Plain-browser dev: no Electron bridge, nothing to notify with.
    if (typeof window.jarvis?.notify !== 'function') return;
    // The user is already looking at Jarvis — the in-app UI carries the event.
    if (document.hasFocus()) return;
    const content = notificationContent(event);
    if (content) {
      window.jarvis.notify(content.title, content.body);
    }
  });
}
