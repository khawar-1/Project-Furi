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
  Database,
  FolderSearch,
  Link2,
  Link2Off,
  Loader2,
  Plus,
  RotateCw,
  Send,
  Settings as SettingsIcon,
  ShieldCheck,
  Sunrise,
  X,
} from 'lucide-react';
import { indexApi, integrationsApi, settingsApi } from '@/lib/api';
import type {
  BriefingSettings,
  FileIndexSettings,
  FrequentFolder,
  GoogleIntegrationStatus,
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
          onClick={() => setEnabled((v) => !v)}
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
      </div>
    </div>
  );
}
