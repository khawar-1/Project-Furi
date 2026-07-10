/**
 * Jarvis OS — Push Channel Client (Phase 4, Part 1)
 *
 * The one WebSocket connection to the backend's /ws endpoint, through which
 * the server initiates messages no request asked for (reminders, task
 * updates, ...). Strictly server→client: this module never sends commands.
 *
 * - Auto-reconnects with exponential backoff (1s → 30s cap) for as long as
 *   connectPush() is in effect; the backoff resets on a successful open.
 * - Events are dispatched by their envelope `type` to handlers registered
 *   via onPush(). Unknown types are ignored — a newer backend can add event
 *   types without breaking this frontend.
 * - onPush('*', handler) receives EVERY event (used later for the native
 *   notification bridge).
 */
import type { PushEvent } from '@/types';
import { getBaseUrl } from '@/lib/api';
import { usePushStore } from '@/stores/pushStore';

type PushHandler = (event: PushEvent) => void;

const MAX_RECONNECT_DELAY_MS = 30_000;
const INITIAL_RECONNECT_DELAY_MS = 1_000;

const handlers = new Map<string, Set<PushHandler>>();

let socket: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let reconnectDelay = INITIAL_RECONNECT_DELAY_MS;
let shouldRun = false;

function wsUrl(): string {
  return getBaseUrl().replace(/^http/, 'ws') + '/ws';
}

/** Register a handler for one event type ('*' = every event).
 *  Returns an unsubscribe function. */
export function onPush(type: string, handler: PushHandler): () => void {
  let set = handlers.get(type);
  if (!set) {
    set = new Set();
    handlers.set(type, set);
  }
  set.add(handler);
  return () => {
    handlers.get(type)?.delete(handler);
  };
}

function dispatch(event: PushEvent): void {
  usePushStore.getState().markEvent(event.ts);
  for (const handler of handlers.get(event.type) ?? []) {
    try {
      handler(event);
    } catch (e) {
      console.error(`Push handler for '${event.type}' threw:`, e);
    }
  }
  for (const handler of handlers.get('*') ?? []) {
    try {
      handler(event);
    } catch (e) {
      console.error('Wildcard push handler threw:', e);
    }
  }
}

function scheduleReconnect(): void {
  if (!shouldRun || reconnectTimer !== null) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY_MS);
    open();
  }, reconnectDelay);
}

function open(): void {
  if (!shouldRun) return;
  if (
    socket &&
    (socket.readyState === WebSocket.OPEN ||
      socket.readyState === WebSocket.CONNECTING)
  ) {
    return;
  }

  const ws = new WebSocket(wsUrl());
  socket = ws;

  ws.onopen = () => {
    reconnectDelay = INITIAL_RECONNECT_DELAY_MS;
    usePushStore.getState().setConnected(true);
  };

  ws.onmessage = (message: MessageEvent<string>) => {
    let event: PushEvent;
    try {
      event = JSON.parse(message.data) as PushEvent;
    } catch {
      return; // malformed frame — ignore
    }
    if (!event || typeof event.type !== 'string') return;
    dispatch(event);
  };

  ws.onclose = () => {
    if (socket === ws) socket = null;
    usePushStore.getState().setConnected(false);
    scheduleReconnect();
  };

  // onerror always precedes onclose — reconnect handling lives in onclose.
  ws.onerror = () => {};
}

/** Open the push channel and keep it open (reconnecting) until
 *  disconnectPush() is called. Safe to call more than once. */
export function connectPush(): void {
  shouldRun = true;
  open();
}

/** Close the push channel and stop reconnecting. */
export function disconnectPush(): void {
  shouldRun = false;
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  reconnectDelay = INITIAL_RECONNECT_DELAY_MS;
  const ws = socket;
  socket = null;
  ws?.close();
  usePushStore.getState().setConnected(false);
}
