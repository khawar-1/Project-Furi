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
  Activity,
  CheckCircle2,
  Database,
  Eye,
  FolderSearch,
  Gauge,
  Link2,
  Link2Off,
  Loader2,
  MessageSquare,
  Mic,
  Monitor,
  Plus,
  RotateCw,
  Send,
  Settings as SettingsIcon,
  ShieldCheck,
  Sparkles,
  Square,
  Sunrise,
  Volume2,
  X,
} from 'lucide-react';
import { useShallow } from 'zustand/react/shallow';
import {
  indexApi,
  initiativeApi,
  integrationsApi,
  settingsApi,
  voiceApi,
  voiceUpdatePayload,
  type VoiceUpdateBody,
} from '@/lib/api';
import { useVoiceStore } from '@/stores/voiceStore';
import { useContextStore } from '@/stores/contextStore';
import { speakText } from '@/lib/voiceOutput';
import { startRecording, voiceCaptureSupported, type RecordingHandle } from '@/lib/voiceInput';
import type {
  BriefingSettings,
  FileIndexSettings,
  FrequentFolder,
  GoogleIntegrationStatus,
  InitiativeSettings,
  SttStatus,
  TtsStatus,
  WorldModel,
} from '@/types';

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

const AUTONOMY_HELP: Record<string, string> = {
  suggest: 'Only shows suggestions — never starts anything on its own.',
  ask: 'May offer to do something; it only runs after you accept.',
  act: 'May start safe actions itself. Anything that sends, deletes, or leaves the machine still asks for approval first.',
};

function InitiativeCard() {
  const [settings, setSettings] = useState<InitiativeSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isBusy, setIsBusy] = useState(false);
  const [justRan, setJustRan] = useState(false);

  useEffect(() => {
    initiativeApi
      .getSettings()
      .then(setSettings)
      .catch((e) => setError(e instanceof Error ? e.message : 'Could not load settings'));
  }, []);

  // Every control PUTs the FULL body immediately (optimistic + settle from the
  // response), the DailyBriefingCard discipline — a partial body would reset the
  // omitted fields. `patch` merges over the last-known settings.
  const patch = async (update: Partial<InitiativeSettings>) => {
    if (!settings) return;
    const merged = { ...settings, ...update };
    setSettings(merged); // optimistic
    setIsBusy(true);
    setError(null);
    try {
      setSettings(await initiativeApi.updateSettings({
        enabled: merged.enabled,
        autonomy: merged.autonomy,
        interval_minutes: merged.interval_minutes,
        daily_budget: merged.daily_budget,
        quiet_start_hour: merged.quiet_start_hour,
        quiet_end_hour: merged.quiet_end_hour,
        min_gap_minutes: merged.min_gap_minutes,
      }));
    } catch (e) {
      setSettings(settings); // revert
      setError(e instanceof Error ? e.message : 'Could not save');
    } finally {
      setIsBusy(false);
    }
  };

  const handleRunNow = async () => {
    setIsBusy(true);
    setError(null);
    try {
      await initiativeApi.runNow();
      setJustRan(true);
      setTimeout(() => setJustRan(false), 2500);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not run');
    } finally {
      setIsBusy(false);
    }
  };

  const enabled = settings?.enabled ?? false;
  const autonomy = settings?.autonomy ?? 'ask';
  const nextRun = settings?.next_run_at ? new Date(settings.next_run_at) : null;

  return (
    <div className="bg-surface-1 border border-surface-border rounded-xl overflow-hidden">
      {/* Card header */}
      <div className="flex items-center gap-3 px-4 py-3.5 border-b border-surface-border">
        <div className="w-8 h-8 rounded-lg bg-surface-2 border border-surface-border flex items-center justify-center text-cyan-400/80">
          <Sparkles size={15} />
        </div>
        <div className="flex-1 min-w-0">
          <h2 className="text-sm font-semibold text-slate-200">Initiative engine</h2>
          <p className="text-xs text-muted truncate">
            {enabled
              ? nextRun
                ? `Next check: ${nextRun.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' })}`
                : `Checking every ${settings?.interval_minutes ?? 45} min`
              : 'Jarvis volunteers helpful suggestions — off'}
          </p>
        </div>
        <button
          role="switch"
          aria-checked={enabled}
          disabled={isBusy || !settings}
          onClick={() => void patch({ enabled: !enabled })}
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

      {/* Card body — only meaningful when enabled */}
      {enabled && settings && (
        <div className="px-4 py-3.5 space-y-3.5">
          <p className="text-xs text-slate-400">
            On a throttled schedule, Jarvis reviews your day (calendar, inbox, notes, and
            activity) and surfaces a few timely suggestions in the Suggestions panel.
          </p>

          {/* Autonomy */}
          <div className="space-y-1.5">
            <label className="text-xs text-slate-400" htmlFor="initiative-autonomy">
              How far it may go
            </label>
            <select
              id="initiative-autonomy"
              value={autonomy}
              disabled={isBusy}
              onChange={(e) => void patch({ autonomy: e.target.value as InitiativeSettings['autonomy'] })}
              className="w-full px-2.5 py-1.5 rounded-lg text-xs bg-surface-2 border border-surface-border text-slate-200 disabled:opacity-40 focus:outline-none focus:border-cyan-500/40"
            >
              <option value="suggest">Suggest only</option>
              <option value="ask">Ask before acting</option>
              <option value="act">Act on safe things</option>
            </select>
            <p className="text-[11px] text-muted leading-relaxed">
              {AUTONOMY_HELP[autonomy] ?? AUTONOMY_HELP.ask}
            </p>
          </div>

          {/* Numeric governors */}
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <label className="text-xs text-slate-400" htmlFor="initiative-budget">
                Max per day
              </label>
              <input
                id="initiative-budget"
                type="number"
                min={0}
                max={50}
                value={settings.daily_budget}
                disabled={isBusy}
                onChange={(e) => void patch({ daily_budget: Number(e.target.value) })}
                className="w-full px-2.5 py-1.5 rounded-lg text-xs bg-surface-2 border border-surface-border text-slate-200 disabled:opacity-40 focus:outline-none focus:border-cyan-500/40"
              />
            </div>
            <div className="space-y-1">
              <label className="text-xs text-slate-400" htmlFor="initiative-interval">
                Check every (min)
              </label>
              <input
                id="initiative-interval"
                type="number"
                min={15}
                max={1440}
                value={settings.interval_minutes}
                disabled={isBusy}
                onChange={(e) => void patch({ interval_minutes: Number(e.target.value) })}
                className="w-full px-2.5 py-1.5 rounded-lg text-xs bg-surface-2 border border-surface-border text-slate-200 disabled:opacity-40 focus:outline-none focus:border-cyan-500/40"
              />
            </div>
            <div className="space-y-1">
              <label className="text-xs text-slate-400" htmlFor="initiative-quiet-start">
                Quiet from (hour)
              </label>
              <input
                id="initiative-quiet-start"
                type="number"
                min={0}
                max={23}
                value={settings.quiet_start_hour}
                disabled={isBusy}
                onChange={(e) => void patch({ quiet_start_hour: Number(e.target.value) })}
                className="w-full px-2.5 py-1.5 rounded-lg text-xs bg-surface-2 border border-surface-border text-slate-200 disabled:opacity-40 focus:outline-none focus:border-cyan-500/40"
              />
            </div>
            <div className="space-y-1">
              <label className="text-xs text-slate-400" htmlFor="initiative-quiet-end">
                Quiet until (hour)
              </label>
              <input
                id="initiative-quiet-end"
                type="number"
                min={0}
                max={23}
                value={settings.quiet_end_hour}
                disabled={isBusy}
                onChange={(e) => void patch({ quiet_end_hour: Number(e.target.value) })}
                className="w-full px-2.5 py-1.5 rounded-lg text-xs bg-surface-2 border border-surface-border text-slate-200 disabled:opacity-40 focus:outline-none focus:border-cyan-500/40"
              />
            </div>
          </div>
          <p className="text-[11px] text-muted">
            During quiet hours Jarvis stays silent. Suggestions are also spaced at least{' '}
            {settings.min_gap_minutes} min apart.
          </p>

          {error && (
            <div className="p-2.5 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs">
              {error}
            </div>
          )}

          <button
            onClick={() => void handleRunNow()}
            disabled={isBusy}
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium text-cyan-400 bg-cyan-500/10 border border-cyan-500/20 hover:bg-cyan-500/20 transition-colors disabled:opacity-40"
          >
            {justRan ? <CheckCircle2 size={12} /> : <Sparkles size={12} />}
            {justRan ? 'Pass run' : 'Run a pass now'}
          </button>
        </div>
      )}

      {!enabled && (
        <div className="px-4 py-3.5">
          <p className="text-xs text-slate-400">
            When on, Jarvis periodically looks for genuinely useful things to raise — a
            meeting to prep for, an email worth a reply — and offers them quietly. Off by
            default; it never acts without your say-so unless you allow it.
          </p>
          {error && (
            <div className="mt-2 p-2.5 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs">
              {error}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** A small editable list of folder paths (used for both folders and exclusions). */
function PathList({
  paths,
  onChange,
  placeholder,
  emptyHint,
  disabled,
}: {
  paths: string[];
  onChange: (next: string[]) => void;
  placeholder: string;
  emptyHint: string;
  disabled?: boolean;
}) {
  const [draft, setDraft] = useState('');

  const add = () => {
    const value = draft.trim();
    if (value && !paths.includes(value)) onChange([...paths, value]);
    setDraft('');
  };

  return (
    <div className="space-y-1.5">
      {paths.length === 0 && <p className="text-[11px] text-slate-600">{emptyHint}</p>}
      {paths.map((p) => (
        <div
          key={p}
          className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg bg-surface-2 border border-surface-border"
        >
          <FolderSearch size={12} className="text-cyan-500/60 flex-shrink-0" />
          <span className="flex-1 min-w-0 text-xs text-slate-300 font-mono truncate" title={p}>
            {p}
          </span>
          <button
            onClick={() => onChange(paths.filter((x) => x !== p))}
            disabled={disabled}
            className="text-slate-600 hover:text-red-400 transition-colors disabled:opacity-40"
            aria-label={`Remove ${p}`}
          >
            <X size={13} />
          </button>
        </div>
      ))}
      <div className="flex items-center gap-2">
        <input
          type="text"
          value={draft}
          disabled={disabled}
          placeholder={placeholder}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              add();
            }
          }}
          className="flex-1 min-w-0 px-2.5 py-1.5 rounded-lg text-xs font-mono bg-surface-2 border border-surface-border text-slate-200 placeholder:text-slate-600 disabled:opacity-40 focus:outline-none focus:border-cyan-500/40"
        />
        <button
          onClick={add}
          disabled={disabled || !draft.trim()}
          className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-medium text-cyan-400 bg-cyan-500/10 border border-cyan-500/20 hover:bg-cyan-500/20 transition-colors disabled:opacity-40"
        >
          <Plus size={12} />
          Add
        </button>
      </div>
    </div>
  );
}

function FileIndexCard() {
  const [config, setConfig] = useState<FileIndexSettings | null>(null);
  const [folders, setFolders] = useState<string[]>([]);
  const [exclusions, setExclusions] = useState<string[]>([]);
  const [interval, setIntervalMinutes] = useState(360);
  const [enabled, setEnabled] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isBusy, setIsBusy] = useState(false);
  const [frequent, setFrequent] = useState<FrequentFolder[]>([]);
  const pollRef = useRef<ReturnType<typeof setTimeout>>();

  // Phase 6 Part 6 — the learned save/move destinations (best-effort display).
  const loadFrequent = useCallback(() => {
    indexApi
      .frequentFolders()
      .then((r) => setFrequent(r.folders))
      .catch(() => setFrequent([]));
  }, []);

  const apply = useCallback((c: FileIndexSettings) => {
    setConfig(c);
    setFolders(c.folders);
    setExclusions(c.exclusions);
    setIntervalMinutes(c.interval_minutes);
    setEnabled(c.enabled);
  }, []);

  useEffect(() => {
    indexApi
      .get()
      .then(apply)
      .catch((e) => setError(e instanceof Error ? e.message : 'Could not load settings'));
    loadFrequent();
    return () => clearTimeout(pollRef.current);
  }, [apply, loadFrequent]);

  const dirty =
    !!config &&
    (enabled !== config.enabled ||
      interval !== config.interval_minutes ||
      JSON.stringify(folders) !== JSON.stringify(config.folders) ||
      JSON.stringify(exclusions) !== JSON.stringify(config.exclusions));

  const save = async () => {
    setIsBusy(true);
    setError(null);
    try {
      apply(
        await indexApi.updateConfig({
          enabled,
          folders,
          exclusions,
          interval_minutes: interval,
        })
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save');
    } finally {
      setIsBusy(false);
    }
  };

  // Poll status while a background pass runs, so counts update live.
  const pollStatus = useCallback(() => {
    clearTimeout(pollRef.current);
    pollRef.current = setTimeout(async () => {
      try {
        const status = await indexApi.status();
        setConfig((c) => (c ? { ...c, status } : c));
        if (status.indexing) pollStatus();
      } catch {
        /* stop polling on error */
      }
    }, 2000);
  }, []);

  // The toggle persists IMMEDIATELY (the DailyBriefingCard behavior). It used
  // to flip local state only and silently require a separate "Save changes"
  // click — the user saw it on, it was never saved, and reopening the panel
  // showed it off again (live bug 2026-07-13). Turning it on also starts an
  // index build server-side, so poll the counts.
  const toggleEnabled = async () => {
    const next = !enabled;
    setEnabled(next); // optimistic — apply() below settles it from the server
    setIsBusy(true);
    setError(null);
    try {
      apply(
        await indexApi.updateConfig({
          enabled: next,
          folders,
          exclusions,
          interval_minutes: interval,
        })
      );
      if (next) pollStatus();
    } catch (e) {
      setEnabled(!next); // revert — the server still holds the old value
      setError(e instanceof Error ? e.message : 'Could not save');
    } finally {
      setIsBusy(false);
    }
  };

  const rebuild = async () => {
    setIsBusy(true);
    setError(null);
    try {
      if (dirty) await save();
      await indexApi.rebuild(false);
      setConfig((c) => (c ? { ...c, status: { ...c.status, indexing: true } } : c));
      pollStatus();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not start indexing');
    } finally {
      setIsBusy(false);
    }
  };

  const status = config?.status;
  const indexing = status?.indexing ?? false;
  const lastIndexed = status?.last_indexed_at ? new Date(status.last_indexed_at) : null;

  return (
    <div className="bg-surface-1 border border-surface-border rounded-xl overflow-hidden">
      {/* Card header */}
      <div className="flex items-center gap-3 px-4 py-3.5 border-b border-surface-border">
        <div className="w-8 h-8 rounded-lg bg-surface-2 border border-surface-border flex items-center justify-center text-cyan-400/80">
          <Database size={15} />
        </div>
        <div className="flex-1 min-w-0">
          <h2 className="text-sm font-semibold text-slate-200">File search index</h2>
          <p className="text-xs text-muted truncate">
            {enabled
              ? `${status?.indexed_files ?? 0} file(s) indexed`
              : 'Search your documents by meaning — off'}
          </p>
        </div>
        <button
          role="switch"
          aria-checked={enabled}
          disabled={isBusy || !config}
          onClick={() => void toggleEnabled()}
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
      <div className="px-4 py-3.5 space-y-4">
        <p className="text-xs text-slate-400">
          Jarvis indexes the text of your documents (.txt, .md, .pdf, .docx) in the folders
          below so you can find them by meaning, not just filename. Indexing runs locally —
          nothing leaves your machine. Whole drives are never indexed.
        </p>

        <div className="space-y-1.5">
          <p className="text-[11px] uppercase tracking-wide text-slate-600">Folders to index</p>
          <PathList
            paths={folders}
            onChange={setFolders}
            disabled={isBusy}
            placeholder="Paste a folder path, e.g. C:\Users\you\Documents"
            emptyHint="No folders yet — add one above."
          />
        </div>

        <div className="space-y-1.5">
          <p className="text-[11px] uppercase tracking-wide text-slate-600">Exclude (sensitive)</p>
          <PathList
            paths={exclusions}
            onChange={setExclusions}
            disabled={isBusy}
            placeholder="Paste a folder path to skip"
            emptyHint="Nothing excluded."
          />
        </div>

        <div className="flex items-center gap-3">
          <label className="text-xs text-slate-400" htmlFor="index-interval">
            Re-index every
          </label>
          <input
            id="index-interval"
            type="number"
            min={15}
            value={interval}
            disabled={isBusy}
            onChange={(e) => setIntervalMinutes(Number(e.target.value) || 15)}
            className="w-20 px-2.5 py-1.5 rounded-lg text-xs bg-surface-2 border border-surface-border text-slate-200 disabled:opacity-40 focus:outline-none focus:border-cyan-500/40"
          />
          <span className="text-xs text-slate-500">minutes</span>
        </div>

        {status && (
          <div className="text-[11px] text-slate-500">
            {status.indexed_chunks} text chunk(s) indexed
            {lastIndexed && ` · updated ${lastIndexed.toLocaleString([], {
              month: 'short',
              day: 'numeric',
              hour: '2-digit',
              minute: '2-digit',
            })}`}
          </div>
        )}

        {frequent.length > 0 && (
          <div className="space-y-1.5 pt-1 border-t border-surface-border/60">
            <p className="text-[11px] uppercase tracking-wide text-slate-600 pt-2.5">
              Folders you use most
            </p>
            <p className="text-[11px] text-slate-500">
              Learned from your past file actions — Jarvis may suggest the top one when
              you save or move a file without saying where (you still approve it).
            </p>
            <ul className="space-y-1">
              {frequent.map((f) => (
                <li
                  key={f.folder}
                  className="flex items-center gap-2 text-xs text-slate-400"
                >
                  <span className="font-mono truncate" title={f.folder}>
                    {f.folder}
                  </span>
                  <span className="ml-auto flex-shrink-0 text-[10px] text-slate-600">
                    {f.count}×
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {error && (
          <div className="p-2.5 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs">
            {error}
          </div>
        )}

        <div className="flex items-center gap-2">
          <button
            onClick={() => void save()}
            disabled={isBusy || !dirty}
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium text-cyan-400 bg-cyan-500/10 border border-cyan-500/20 hover:bg-cyan-500/20 transition-colors disabled:opacity-40"
          >
            {isBusy ? <Loader2 size={12} className="animate-spin" /> : <CheckCircle2 size={12} />}
            {dirty ? 'Save changes' : 'Saved'}
          </button>
          <button
            onClick={() => void rebuild()}
            disabled={isBusy || indexing || !enabled}
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium text-slate-300 bg-surface-2 border border-surface-border hover:border-cyan-500/30 transition-colors disabled:opacity-40"
            title={!enabled ? 'Enable the index first' : undefined}
          >
            <RotateCw size={12} className={indexing ? 'animate-spin' : ''} />
            {indexing ? 'Indexing…' : 'Index now'}
          </button>
        </div>
      </div>
    </div>
  );
}

/** The shared switch control (the DailyBriefingCard/FileIndexCard toggle). */
function ToggleSwitch({
  checked,
  disabled,
  onToggle,
  label,
}: {
  checked: boolean;
  disabled?: boolean;
  onToggle: () => void;
  label: string;
}) {
  return (
    <button
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={onToggle}
      className={clsx(
        'relative w-10 h-5 rounded-full transition-colors flex-shrink-0 disabled:opacity-40',
        checked ? 'bg-cyan-500/70' : 'bg-surface-2 border border-surface-border'
      )}
    >
      <span
        className={clsx(
          'absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform',
          checked ? 'translate-x-5' : 'translate-x-0.5'
        )}
      />
    </button>
  );
}

/** One model status line: name + state chip + a progress bar while
 *  downloading. The bar is HONEST: a real percentage only when the backend
 *  reports one (`progress`), otherwise an indeterminate bar — the model
 *  downloads here are opaque, so we never fabricate a percentage. */
/** Device id → human label for the pickers and status badges. */
const DEVICE_LABELS: Record<string, string> = {
  auto: 'Auto',
  cpu: 'CPU',
  cuda: 'GPU',
};

function ModelStatusRow({
  title,
  status,
}: {
  title: string;
  status: (SttStatus | TtsStatus) & { progress?: TtsStatus['progress']; device?: string | null };
}) {
  const progress = status.progress ?? null;
  const percent =
    progress && progress.total ? Math.min(100, Math.round((progress.downloaded / progress.total) * 100)) : null;
  // When ready, show which device it actually loaded on (GPU vs CPU) — makes
  // "Auto" honest about what it picked.
  const deviceLabel =
    status.status === 'ready' && status.device
      ? DEVICE_LABELS[status.device] ?? status.device
      : null;
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        <span className="flex-1 min-w-0 text-xs text-slate-400 truncate">{title}</span>
        {deviceLabel && (
          <span
            className={clsx(
              'text-[10px] px-1.5 py-0.5 rounded-full border font-mono flex-shrink-0',
              status.device === 'cuda'
                ? 'bg-cyan-500/10 text-cyan-400 border-cyan-500/20'
                : 'bg-surface-2 text-slate-500 border-surface-border'
            )}
          >
            {deviceLabel}
          </span>
        )}
        <span
          className={clsx(
            'text-[10px] px-2 py-0.5 rounded-full border font-mono flex items-center gap-1.5 flex-shrink-0',
            status.status === 'ready'
              ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
              : status.status === 'loading'
                ? 'bg-amber-500/10 text-amber-400 border-amber-500/20'
                : status.status === 'error'
                  ? 'bg-red-500/10 text-red-400 border-red-500/20'
                  : 'bg-surface-2 text-slate-500 border-surface-border'
          )}
        >
          {status.status === 'loading' && <Loader2 size={10} className="animate-spin" />}
          {status.status === 'loading'
            ? percent !== null
              ? `downloading ${percent}%`
              : 'downloading…'
            : status.status === 'not_loaded'
              ? 'not loaded'
              : status.status}
        </span>
      </div>
      {status.status === 'loading' && (
        <div className="h-1 rounded-full bg-surface-2 overflow-hidden">
          {percent !== null ? (
            <div
              className="h-full bg-cyan-500/70 transition-[width] duration-500"
              style={{ width: `${percent}%` }}
            />
          ) : (
            <div className="h-full w-1/3 bg-cyan-500/40 animate-pulse" />
          )}
        </div>
      )}
      {status.status === 'error' && status.error && (
        <p className="text-[11px] text-red-400/80 break-words">{status.error}</p>
      )}
    </div>
  );
}

function VoiceCard() {
  const { settings, phase, applySettings, fetchSettings } = useVoiceStore(
    useShallow((state) => ({
      settings: state.settings,
      phase: state.phase,
      applySettings: state.applySettings,
      fetchSettings: state.fetchSettings,
    }))
  );
  const [isBusy, setIsBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Test mic — deliberately card-local, never the voiceStore phase machine:
  // a test recording must never route into chat.
  const [testState, setTestState] = useState<'idle' | 'recording' | 'transcribing'>('idle');
  const [testText, setTestText] = useState<string | null>(null);
  const testHandleRef = useRef<RecordingHandle | null>(null);

  useEffect(() => {
    void fetchSettings(); // refresh on open — App.tsx fetched at startup
    return () => {
      testHandleRef.current?.cancel();
    };
  }, [fetchSettings]);

  /** Every control PUTs IMMEDIATELY (the FileIndexCard enable-flow lesson):
   *  optimistic through the shared store (ChatPanel reacts live), settled by
   *  the server response, reverted on failure. */
  const patch = async (update: Partial<VoiceUpdateBody>) => {
    if (!settings || isBusy) return;
    setIsBusy(true);
    setError(null);
    applySettings({ ...settings, ...update });
    try {
      applySettings(await voiceApi.updateSettings(voiceUpdatePayload(settings, update)));
    } catch (e) {
      applySettings(settings); // revert — the backend never saw it
      setError(e instanceof Error ? e.message : 'Could not save');
    } finally {
      setIsBusy(false);
    }
  };

  const toggleTestMic = async () => {
    if (testState === 'recording') {
      const handle = testHandleRef.current;
      testHandleRef.current = null;
      setTestState('transcribing');
      try {
        const blob = await handle?.stop();
        if (!blob) {
          setTestState('idle');
          return;
        }
        const result = await voiceApi.transcribe(blob);
        setTestText(result.text.trim() || '(nothing heard)');
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Transcription failed');
      } finally {
        setTestState('idle');
      }
      return;
    }
    setError(null);
    setTestText(null);
    try {
      testHandleRef.current = await startRecording({
        onAutoStop: () => void toggleTestMic(),
      });
      setTestState('recording');
    } catch (e) {
      setError(
        e instanceof Error && e.name === 'NotAllowedError'
          ? 'Microphone access was denied.'
          : e instanceof Error
            ? e.message
            : 'Could not start the microphone.'
      );
    }
  };

  const enabled = settings?.enabled ?? false;
  const sttReady = settings?.stt_status.status === 'ready';
  const ttsReady = settings?.tts_status.status === 'ready';
  const toggles: Array<{
    key: keyof VoiceUpdateBody;
    label: string;
    hint: string;
    value: boolean;
  }> = settings
    ? [
        {
          key: 'output_enabled',
          label: 'Spoken replies',
          hint: 'Jarvis reads its answers aloud (also the header speaker toggle)',
          value: settings.output_enabled,
        },
        {
          key: 'review_before_send',
          label: 'Review before send',
          hint: 'Transcripts land in the input box instead of auto-sending',
          value: settings.review_before_send,
        },
        {
          key: 'speak_all_responses',
          label: 'Speak typed responses too',
          hint: 'Replies to typed messages are spoken, not just voice turns',
          value: settings.speak_all_responses,
        },
        {
          key: 'speak_proactive',
          label: 'Speak notifications aloud',
          hint: 'Reminders, briefings, and task outcomes are announced by voice',
          value: settings.speak_proactive,
        },
        {
          key: 'listen_on_summon',
          label: 'Listen when summoned',
          hint: 'Ctrl+Shift+J starts listening hands-free; pausing sends',
          value: settings.listen_on_summon,
        },
        {
          key: 'continuous_conversation',
          label: 'Continuous conversation',
          hint: 'After a spoken reply, keep listening briefly so you can talk back without re-triggering',
          value: settings.continuous_conversation,
        },
        {
          key: 'wake_word',
          label: 'Wake word (“Hey Jarvis”)',
          hint: 'Always-on microphone; detection runs on-device and audio never leaves this machine',
          value: settings.wake_word,
        },
      ]
    : [];

  return (
    <div className="bg-surface-1 border border-surface-border rounded-xl overflow-hidden">
      {/* Card header */}
      <div className="flex items-center gap-3 px-4 py-3.5 border-b border-surface-border">
        <div className="w-8 h-8 rounded-lg bg-surface-2 border border-surface-border flex items-center justify-center text-cyan-400/80">
          <Mic size={15} />
        </div>
        <div className="flex-1 min-w-0">
          <h2 className="text-sm font-semibold text-slate-200">Voice</h2>
          <p className="text-xs text-muted truncate">
            {enabled
              ? 'Push-to-talk and spoken replies — everything runs locally'
              : 'Talk to Jarvis and hear it answer — off'}
          </p>
        </div>
        <ToggleSwitch
          checked={enabled}
          disabled={isBusy || !settings}
          onToggle={() => void patch({ enabled: !enabled })}
          label="Voice on/off"
        />
      </div>

      {/* Card body */}
      <div className="px-4 py-3.5 space-y-4">
        <p className="text-xs text-slate-400">
          Speech recognition (Whisper) and speech synthesis (Kokoro) both run on this
          machine — audio never leaves it. Enabling downloads the models once. With
          “Auto” device, they run on the GPU when one is available (much faster),
          otherwise the CPU.
        </p>

        {/* Behavior toggles */}
        <div className="space-y-2.5">
          {toggles.map((t) => (
            <div key={t.key} className="flex items-center gap-3">
              <div className="flex-1 min-w-0">
                <p className="text-xs text-slate-300">{t.label}</p>
                <p className="text-[11px] text-slate-600 truncate" title={t.hint}>
                  {t.hint}
                </p>
              </div>
              <ToggleSwitch
                checked={t.value}
                disabled={isBusy || !settings || !enabled}
                onToggle={() => void patch({ [t.key]: !t.value })}
                label={t.label}
              />
            </div>
          ))}
        </div>

        {/* Recognition model + speaking voice + speed */}
        <div className="flex flex-wrap items-center gap-3">
          <label className="text-xs text-slate-400" htmlFor="voice-stt-model">
            Recognition model
          </label>
          <select
            id="voice-stt-model"
            value={settings?.stt_model ?? 'small'}
            disabled={isBusy || !settings}
            onChange={(e) => void patch({ stt_model: e.target.value })}
            className="px-2.5 py-1.5 rounded-lg text-xs bg-surface-2 border border-surface-border text-slate-200 disabled:opacity-40 focus:outline-none focus:border-cyan-500/40"
          >
            {(settings?.stt_models ?? []).map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
          <label className="text-xs text-slate-400" htmlFor="voice-tts-voice">
            Voice
          </label>
          <select
            id="voice-tts-voice"
            value={settings?.voice ?? 'af_heart'}
            disabled={isBusy || !settings}
            onChange={(e) => void patch({ voice: e.target.value })}
            className="px-2.5 py-1.5 rounded-lg text-xs bg-surface-2 border border-surface-border text-slate-200 disabled:opacity-40 focus:outline-none focus:border-cyan-500/40"
          >
            {(settings?.voices ?? []).map((v) => (
              <option key={v.id} value={v.id}>
                {v.label}
              </option>
            ))}
          </select>
          <label className="text-xs text-slate-400" htmlFor="voice-tts-speed">
            Speed
          </label>
          <select
            id="voice-tts-speed"
            value={String(settings?.tts_speed ?? 1.0)}
            disabled={isBusy || !settings}
            onChange={(e) => void patch({ tts_speed: parseFloat(e.target.value) })}
            className="px-2.5 py-1.5 rounded-lg text-xs bg-surface-2 border border-surface-border text-slate-200 disabled:opacity-40 focus:outline-none focus:border-cyan-500/40"
          >
            <option value="0.75">0.75× (slower)</option>
            <option value="1">1× (normal)</option>
            <option value="1.25">1.25×</option>
            <option value="1.5">1.5× (faster)</option>
          </select>
        </div>

        {/* Device selection — GPU makes both engines near-instant; Auto picks
            the GPU when present, else CPU. A change reloads that engine. */}
        <div className="flex flex-wrap items-center gap-3">
          <label className="text-xs text-slate-400" htmlFor="voice-stt-device">
            Recognition runs on
          </label>
          <select
            id="voice-stt-device"
            value={settings?.stt_device ?? 'auto'}
            disabled={isBusy || !settings}
            onChange={(e) => void patch({ stt_device: e.target.value })}
            className="px-2.5 py-1.5 rounded-lg text-xs bg-surface-2 border border-surface-border text-slate-200 disabled:opacity-40 focus:outline-none focus:border-cyan-500/40"
          >
            {(settings?.devices ?? ['auto', 'cpu', 'cuda']).map((d) => (
              <option key={d} value={d}>
                {DEVICE_LABELS[d] ?? d}
              </option>
            ))}
          </select>
          <label className="text-xs text-slate-400" htmlFor="voice-tts-device">
            Speech runs on
          </label>
          <select
            id="voice-tts-device"
            value={settings?.tts_device ?? 'auto'}
            disabled={isBusy || !settings}
            onChange={(e) => void patch({ tts_device: e.target.value })}
            className="px-2.5 py-1.5 rounded-lg text-xs bg-surface-2 border border-surface-border text-slate-200 disabled:opacity-40 focus:outline-none focus:border-cyan-500/40"
          >
            {(settings?.devices ?? ['auto', 'cpu', 'cuda']).map((d) => (
              <option key={d} value={d}>
                {DEVICE_LABELS[d] ?? d}
              </option>
            ))}
          </select>
        </div>

        {/* Model status (live via the store's status poller) */}
        {settings && (
          <div className="space-y-2">
            <ModelStatusRow
              title={`Speech recognition — ${settings.stt_model}`}
              status={settings.stt_status}
            />
            <ModelStatusRow title="Speaking engine (Kokoro)" status={settings.tts_status} />
          </div>
        )}

        {error && (
          <div className="p-2.5 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs">
            {error}
          </div>
        )}

        {/* Try it out */}
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <button
              onClick={() => void toggleTestMic()}
              disabled={
                !enabled ||
                !sttReady ||
                !voiceCaptureSupported() ||
                testState === 'transcribing' ||
                (phase !== 'idle' && testState === 'idle')
              }
              className={clsx(
                'flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors disabled:opacity-40',
                testState === 'recording'
                  ? 'text-red-400 bg-red-500/10 border-red-500/20 hover:bg-red-500/20'
                  : 'text-cyan-400 bg-cyan-500/10 border-cyan-500/20 hover:bg-cyan-500/20'
              )}
              title={!sttReady ? 'The recognition model is not ready yet' : undefined}
            >
              {testState === 'recording' ? (
                <Square size={12} fill="currentColor" />
              ) : testState === 'transcribing' ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <Mic size={12} />
              )}
              {testState === 'recording'
                ? 'Stop test'
                : testState === 'transcribing'
                  ? 'Transcribing…'
                  : 'Test mic'}
            </button>
            <button
              onClick={() => speakText("Hi — I'm Jarvis. This is how I sound.")}
              disabled={!enabled || !settings?.output_enabled || !ttsReady}
              className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium text-slate-300 bg-surface-2 border border-surface-border hover:border-cyan-500/30 transition-colors disabled:opacity-40"
              title={
                !ttsReady
                  ? 'The speaking voice is not ready yet'
                  : !settings?.output_enabled
                    ? 'Spoken replies are off'
                    : undefined
              }
            >
              <Volume2 size={12} />
              Speak sample
            </button>
          </div>
          {testText !== null && (
            <p className="text-xs text-slate-300 px-2.5 py-1.5 rounded-lg bg-surface-2 border border-surface-border font-mono">
              “{testText}”
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

const PRESENCE_LABELS: Record<WorldModel['presence'], string> = {
  active: 'At the keyboard',
  idle: 'Idle',
  away: 'Away',
  unknown: 'Unknown',
};

/** Phase 8 — the Context Layer's privacy-first sensing controls + the
 *  "what Jarvis currently sees" audit surface. Every toggle PUTs immediately
 *  (optimistic + revert, via the context store). */
function ContextSensingCard() {
  const {
    settings,
    world,
    screenPaused,
    error,
    fetchSettings,
    updateSettings,
    fetchWorld,
    pauseScreen,
    resumeScreen,
  } = useContextStore(
    useShallow((s) => ({
      settings: s.settings,
      world: s.world,
      screenPaused: s.screenPaused,
      error: s.error,
      fetchSettings: s.fetchSettings,
      updateSettings: s.updateSettings,
      fetchWorld: s.fetchWorld,
      pauseScreen: s.pauseScreen,
      resumeScreen: s.resumeScreen,
    }))
  );

  useEffect(() => {
    void fetchSettings();
  }, [fetchSettings]);

  // While sensing is on, refresh the audit view so the user can see exactly
  // what is stored (the trust surface). Off → don't poll.
  const enabled = settings?.enabled ?? false;
  useEffect(() => {
    if (!enabled) return;
    void fetchWorld();
    const t = setInterval(() => void fetchWorld(), 5_000);
    return () => clearInterval(t);
  }, [enabled, fetchWorld]);

  const deviceSensing = settings?.device_sensing ?? true;
  const screenOcr = settings?.screen_ocr ?? false;
  const screenInChat = settings?.screen_in_chat ?? false;
  const affectiveSensing = settings?.affective_sensing ?? false;
  const ocrInterval = settings?.ocr_interval_seconds ?? 30;
  const idleThreshold = settings?.idle_threshold_seconds ?? 300;
  const hasBridge = typeof window.jarvis?.startScreenSensing === 'function';

  return (
    <div className="bg-surface-1 border border-surface-border rounded-xl overflow-hidden">
      {/* Header + master kill switch */}
      <div className="flex items-center gap-3 px-4 py-3.5 border-b border-surface-border">
        <div className="w-8 h-8 rounded-lg bg-surface-2 border border-surface-border flex items-center justify-center text-emerald-400/80">
          <Eye size={15} />
        </div>
        <div className="flex-1 min-w-0">
          <h2 className="text-sm font-semibold text-slate-200">Context & sensing</h2>
          <p className="text-xs text-muted truncate">
            {enabled ? 'Sensing on — local only, nothing stored' : 'Off — Jarvis senses nothing'}
          </p>
        </div>
        <ToggleSwitch
          checked={enabled}
          disabled={!settings}
          onToggle={() => void updateSettings({ enabled: !enabled })}
          label="Master sensing switch"
        />
      </div>

      {/* Body */}
      <div className="px-4 py-3.5 space-y-3">
        <p className="text-xs text-slate-400">
          Lets Jarvis know what you're doing right now — presence, the active app, and
          (optionally) what's on screen — so it can be genuinely helpful later. Everything
          stays on this machine, nothing is saved to disk, and this master switch turns it
          all off instantly.
        </p>

        {enabled && (
          <>
            {/* Device sensing */}
            <div className="flex items-center gap-3">
              <Activity size={14} className="text-cyan-400/70 flex-shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-xs text-slate-300">Presence & active app</p>
                <p className="text-[11px] text-muted">Active window title + idle time (no screenshots)</p>
              </div>
              <ToggleSwitch
                checked={deviceSensing}
                onToggle={() => void updateSettings({ device_sensing: !deviceSensing })}
                label="Device sensing"
              />
            </div>

            {/* Screen OCR capability */}
            <div className="flex items-center gap-3">
              <Monitor size={14} className="text-amber-400/70 flex-shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-xs text-slate-300">Read the screen (OCR)</p>
                <p className="text-[11px] text-muted">
                  On-screen text, captured automatically and read locally while this is on.
                </p>
              </div>
              <ToggleSwitch
                checked={screenOcr}
                onToggle={() => void updateSettings({ screen_ocr: !screenOcr })}
                label="Screen OCR capability"
              />
            </div>

            {/* Screen-aware chat — opt-in on top of the OCR capability */}
            {screenOcr && (
              <div className="flex items-center gap-3 pl-6">
                <MessageSquare size={14} className="text-amber-400/70 flex-shrink-0" />
                <div className="flex-1 min-w-0">
                  <p className="text-xs text-slate-300">Screen-aware chat</p>
                  <p className="text-[11px] text-muted">
                    Let Jarvis use on-screen text to answer your questions (sends screen
                    text to the language model when you chat).
                  </p>
                </div>
                <ToggleSwitch
                  checked={screenInChat}
                  onToggle={() => void updateSettings({ screen_in_chat: !screenInChat })}
                  label="Screen-aware chat"
                />
              </div>
            )}

            {screenOcr && hasBridge && (
              <button
                onClick={() => (screenPaused ? resumeScreen() : pauseScreen())}
                className={clsx(
                  'flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors',
                  screenPaused
                    ? 'text-amber-400 bg-amber-500/10 border-amber-500/20 hover:bg-amber-500/20'
                    : 'text-red-400 bg-red-500/10 border-red-500/20 hover:bg-red-500/20'
                )}
              >
                {screenPaused ? <Eye size={12} /> : <Square size={12} />}
                {screenPaused ? 'Resume screen capture' : 'Pause screen capture (this session)'}
              </button>
            )}

            {/* Affective sensing (Phase 13) — its own opt-in, highest uncertainty */}
            <div className="flex items-center gap-3">
              <Gauge size={14} className="text-fuchsia-400/70 flex-shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-xs text-slate-300">Read my load (experimental)</p>
                <p className="text-[11px] text-muted">
                  A coarse calm/busy/stressed read from typing pace + voice energy — used only
                  to be briefer and hold non-urgent nudges. Timing only, never keystrokes or audio.
                </p>
              </div>
              <ToggleSwitch
                checked={affectiveSensing}
                onToggle={() => void updateSettings({ affective_sensing: !affectiveSensing })}
                label="Affective sensing"
              />
            </div>

            {/* Cadences */}
            <div className="flex flex-wrap items-center gap-4 pt-1">
              <div className="flex items-center gap-2">
                <label className="text-[11px] text-slate-400" htmlFor="ocr-interval">Screen every (s)</label>
                <input
                  id="ocr-interval"
                  type="number"
                  min={5}
                  value={ocrInterval}
                  onChange={(e) => void updateSettings({ ocr_interval_seconds: Number(e.target.value) })}
                  className="w-16 px-2 py-1 rounded-lg text-xs bg-surface-2 border border-surface-border text-slate-200 focus:outline-none focus:border-cyan-500/40"
                />
              </div>
              <div className="flex items-center gap-2">
                <label className="text-[11px] text-slate-400" htmlFor="idle-threshold">Idle after (s)</label>
                <input
                  id="idle-threshold"
                  type="number"
                  min={30}
                  value={idleThreshold}
                  onChange={(e) => void updateSettings({ idle_threshold_seconds: Number(e.target.value) })}
                  className="w-16 px-2 py-1 rounded-lg text-xs bg-surface-2 border border-surface-border text-slate-200 focus:outline-none focus:border-cyan-500/40"
                />
              </div>
            </div>

            {/* Audit: what Jarvis currently sees */}
            <div className="mt-1 p-3 rounded-lg bg-surface-2/50 border border-surface-border space-y-1.5">
              <div className="flex items-center gap-2 text-[10px] uppercase tracking-wide text-slate-500">
                <ShieldCheck size={11} /> What Jarvis currently sees
              </div>
              <AuditRow label="Presence" value={world ? PRESENCE_LABELS[world.presence] : '—'} />
              <AuditRow label="Active app" value={world?.active_app || '—'} />
              <AuditRow label="Window" value={world?.window_title || '—'} />
              <AuditRow
                label="Next event"
                value={world?.next_calendar_event?.summary
                  ? `${world.next_calendar_event.summary}${world.next_calendar_event.when ? ` — ${world.next_calendar_event.when}` : ''}`
                  : '—'}
              />
              <AuditRow
                label="Unread"
                value={world?.unread ? `${world.unread.count}${world.unread.has_urgent ? ' (urgent)' : ''}` : '—'}
              />
              <AuditRow label="On screen" value={world?.on_screen_context || '—'} />
              {affectiveSensing && (
                <AuditRow
                  label="Load"
                  value={world?.user_state
                    ? `${world.user_state.load} (confidence ${Math.round(world.user_state.confidence * 100)}%)`
                    : '—'}
                />
              )}
            </div>
          </>
        )}

        {error && (
          <div className="p-2.5 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs">
            {error}
          </div>
        )}
      </div>
    </div>
  );
}

function AuditRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-start gap-2 text-[11px]">
      <span className="text-slate-500 w-20 flex-shrink-0">{label}</span>
      <span className="text-slate-300 min-w-0 break-words">{value}</span>
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
        <p className="text-[10px] uppercase tracking-wide text-slate-600 px-1 pt-2">Files</p>
        <FileIndexCard />
        <p className="text-[10px] uppercase tracking-wide text-slate-600 px-1 pt-2">Proactive</p>
        <DailyBriefingCard />
        <InitiativeCard />
        <p className="text-[10px] uppercase tracking-wide text-slate-600 px-1 pt-2">Voice</p>
        <VoiceCard />
        <p className="text-[10px] uppercase tracking-wide text-slate-600 px-1 pt-2">Awareness</p>
        <ContextSensingCard />
      </div>
    </div>
  );
}
