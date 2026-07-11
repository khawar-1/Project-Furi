/**
 * Jarvis OS — Settings Panel (Phase 5, Part 1)
 * Home of external integrations; today that's the Google account card.
 * Connect opens the OAuth consent in the system browser (the backend runs
 * the loopback flow); while it's open we poll status — the status endpoint
 * is purely local on the backend, so fast polling is free.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { clsx } from 'clsx';
import {
  CheckCircle2,
  Link2,
  Link2Off,
  Loader2,
  Send,
  Settings as SettingsIcon,
  ShieldCheck,
  Sunrise,
} from 'lucide-react';
import { integrationsApi, settingsApi } from '@/lib/api';
import type { BriefingSettings, GoogleIntegrationStatus } from '@/types';

const IDLE_POLL_MS = 15_000;
const CONNECTING_POLL_MS = 2_000;

const SCOPE_SUMMARY = [
  'Read and search your Gmail inbox',
  'Create drafts and send email (each send needs your approval)',
  'Create and manage calendar events',
];

function GoogleAccountCard() {
  const [status, setStatus] = useState<GoogleIntegrationStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isBusy, setIsBusy] = useState(false);
  const statusRef = useRef<GoogleIntegrationStatus | null>(null);
  statusRef.current = status;

  const refresh = useCallback(async () => {
    try {
      setStatus(await integrationsApi.googleStatus());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not reach the backend');
    }
  }, []);

  // Poll: slow while idle, fast while a consent flow is open in the browser.
  useEffect(() => {
    void refresh();
    let timer: ReturnType<typeof setTimeout>;
    const tick = () => {
      timer = setTimeout(async () => {
        await refresh();
        tick();
      }, statusRef.current?.connecting ? CONNECTING_POLL_MS : IDLE_POLL_MS);
    };
    tick();
    return () => clearTimeout(timer);
  }, [refresh]);

  const handleConnect = async () => {
    setIsBusy(true);
    setError(null);
    try {
      await integrationsApi.googleConnect();
      await refresh(); // pick up connecting=true so polling speeds up
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Connect failed');
    } finally {
      setIsBusy(false);
    }
  };

  const handleDisconnect = async () => {
    setIsBusy(true);
    setError(null);
    try {
      await integrationsApi.googleDisconnect();
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Disconnect failed');
    } finally {
      setIsBusy(false);
    }
  };

  const connected = status?.connected ?? false;
  const connecting = status?.connecting ?? false;

  return (
    <div className="bg-surface-1 border border-surface-border rounded-xl overflow-hidden">
      {/* Card header */}
      <div className="flex items-center gap-3 px-4 py-3.5 border-b border-surface-border">
        <div className="w-8 h-8 rounded-lg bg-surface-2 border border-surface-border flex items-center justify-center text-cyan-400/80">
          <Link2 size={15} />
        </div>
        <div className="flex-1 min-w-0">
          <h2 className="text-sm font-semibold text-slate-200">Google account</h2>
          <p className="text-xs text-muted truncate">
            {connected
              ? `Connected${status?.account_email ? ` as ${status.account_email}` : ''}`
              : connecting
                ? 'Waiting for you to finish in the browser…'
                : 'Powers email and calendar features'}
          </p>
        </div>
        <span
          className={clsx(
            'text-[10px] px-2 py-0.5 rounded-full border font-mono flex items-center gap-1.5',
            connected
              ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
              : connecting
                ? 'bg-amber-500/10 text-amber-400 border-amber-500/20'
                : 'bg-surface-2 text-slate-500 border-surface-border'
          )}
        >
          {connecting && <Loader2 size={10} className="animate-spin" />}
          {connected ? 'connected' : connecting ? 'connecting' : 'off'}
        </span>
      </div>

      {/* Card body */}
      <div className="px-4 py-3.5 space-y-3">
        <div className="space-y-1.5">
          {SCOPE_SUMMARY.map((line) => (
            <div key={line} className="flex items-center gap-2 text-xs text-slate-400">
              <ShieldCheck size={12} className="text-cyan-500/60 flex-shrink-0" />
              {line}
            </div>
          ))}
          <p className="text-[11px] text-slate-600 pt-1">
            Jarvis never deletes or relabels mail — those permissions are not requested.
            The sign-in token stays on this machine.
          </p>
        </div>

        {(error || status?.detail) && !connected && (
          <div className="p-2.5 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs">
            {error ?? status?.detail}
          </div>
        )}

        <div className="flex items-center gap-2">
          {connected ? (
            <button
              onClick={() => void handleDisconnect()}
              disabled={isBusy}
              className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium text-slate-400 bg-surface-2 border border-surface-border hover:text-red-400 hover:border-red-500/30 transition-colors disabled:opacity-40"
            >
              <Link2Off size={12} />
              Disconnect
            </button>
          ) : (
            <button
              onClick={() => void handleConnect()}
              disabled={isBusy || connecting || !(status?.configured ?? false)}
              className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium text-cyan-400 bg-cyan-500/10 border border-cyan-500/20 hover:bg-cyan-500/20 transition-colors disabled:opacity-40"
              title={
                status?.configured === false
                  ? 'Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env first'
                  : undefined
              }
            >
              {connecting ? <Loader2 size={12} className="animate-spin" /> : <CheckCircle2 size={12} />}
              {connecting ? 'Finish in browser…' : 'Connect Google'}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function DailyBriefingCard() {
  const [settings, setSettings] = useState<BriefingSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isBusy, setIsBusy] = useState(false);
  const [justSent, setJustSent] = useState(false);

  useEffect(() => {
    settingsApi
      .getBriefing()
      .then(setSettings)
      .catch((e) => setError(e instanceof Error ? e.message : 'Could not load settings'));
  }, []);

  const save = async (update: { enabled: boolean; time: string }) => {
    setIsBusy(true);
    setError(null);
    try {
      setSettings(await settingsApi.updateBriefing(update));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save');
    } finally {
      setIsBusy(false);
    }
  };

  const handleSendNow = async () => {
    setIsBusy(true);
    setError(null);
    try {
      await settingsApi.runBriefingNow();
      setJustSent(true);
      setTimeout(() => setJustSent(false), 2500);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not send');
    } finally {
      setIsBusy(false);
    }
  };

  const enabled = settings?.enabled ?? false;
  const time = settings?.time ?? '08:00';
  const nextRun = settings?.next_run_at ? new Date(settings.next_run_at) : null;

  return (
    <div className="bg-surface-1 border border-surface-border rounded-xl overflow-hidden">
      {/* Card header */}
      <div className="flex items-center gap-3 px-4 py-3.5 border-b border-surface-border">
        <div className="w-8 h-8 rounded-lg bg-surface-2 border border-surface-border flex items-center justify-center text-amber-400/80">
          <Sunrise size={15} />
        </div>
        <div className="flex-1 min-w-0">
          <h2 className="text-sm font-semibold text-slate-200">Daily briefing</h2>
          <p className="text-xs text-muted truncate">
            {enabled
              ? nextRun
                ? `Next: ${nextRun.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' })}`
                : `Every day at ${time}`
              : 'A morning summary of your day — off'}
          </p>
        </div>
        {/* Enable toggle */}
        <button
          role="switch"
          aria-checked={enabled}
          disabled={isBusy || !settings}
          onClick={() => void save({ enabled: !enabled, time })}
          className={clsx(
            'relative w-10 h-5 rounded-full transition-colors flex-shrink-0 disabled:opacity-40',
            enabled ? 'bg-cyan-500/70' : 'bg-surface-2 border border-surface-border'
          )}
        >
          <span
            className={clsx(
              'absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform',
              enabled ? 'translate-x-5' : 'translate-x-0.5'
            )}
          />
        </button>
      </div>

      {/* Card body */}
      <div className="px-4 py-3.5 space-y-3">
        <p className="text-xs text-slate-400">
          Each morning Jarvis gathers today's calendar, unread email, birthdays, and notes
          into one message. Read-only — nothing is sent or changed.
        </p>

        <div className="flex items-center gap-3">
          <label className="text-xs text-slate-400" htmlFor="briefing-time">
            Time
          </label>
          <input
            id="briefing-time"
            type="time"
            value={time}
            disabled={isBusy || !settings || !enabled}
            onChange={(e) => void save({ enabled, time: e.target.value })}
            className="px-2.5 py-1.5 rounded-lg text-xs bg-surface-2 border border-surface-border text-slate-200 disabled:opacity-40 focus:outline-none focus:border-cyan-500/40"
          />
        </div>

        {error && (
          <div className="p-2.5 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs">
            {error}
          </div>
        )}

        <button
          onClick={() => void handleSendNow()}
          disabled={isBusy}
          className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium text-cyan-400 bg-cyan-500/10 border border-cyan-500/20 hover:bg-cyan-500/20 transition-colors disabled:opacity-40"
        >
          {justSent ? <CheckCircle2 size={12} /> : <Send size={12} />}
          {justSent ? 'Briefing sent' : 'Send now'}
        </button>
      </div>
    </div>
  );
}

export function SettingsPanel() {
  return (
    <div className="flex flex-col h-full bg-surface overflow-hidden">
      {/* ── Header ── */}
      <div className="flex-shrink-0 px-6 py-4 border-b border-surface-border bg-surface-1/50">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-cyan-500/20 to-blue-600/20 border border-cyan-500/20 flex items-center justify-center">
            <SettingsIcon size={16} className="text-cyan-400" />
          </div>
          <div>
            <h1 className="text-base font-semibold text-slate-200">Settings</h1>
            <p className="text-xs text-muted">Integrations and app configuration</p>
          </div>
        </div>
      </div>

      {/* ── Body ── */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-3 max-w-2xl">
        <p className="text-[10px] uppercase tracking-wide text-slate-600 px-1">Integrations</p>
        <GoogleAccountCard />
        <p className="text-[10px] uppercase tracking-wide text-slate-600 px-1 pt-2">Proactive</p>
        <DailyBriefingCard />
      </div>
    </div>
  );
}
