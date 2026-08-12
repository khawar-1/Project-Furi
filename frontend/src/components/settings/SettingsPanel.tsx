/**
 * Furi OS — Settings Panel (Phase 5, Part 1)
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
  Chrome,
  Database,
  Eye,
  FolderSearch,
  Gauge,
  Home,
  Link2,
  Link2Off,
  Loader2,
  MessageSquare,
  Mic,
  Monitor,
  Plus,
  QrCode,
  RotateCw,
  Send,
  Settings as SettingsIcon,
  ShieldCheck,
  Smartphone,
  Sparkles,
  Square,
  Sunrise,
  Trash2,
  Volume2,
  X,
} from 'lucide-react';
import { useShallow } from 'zustand/react/shallow';
import {
  autofillApi,
  browserApi,
  desktopApi,
  homeApi,
  indexApi,
  initiativeApi,
  integrationsApi,
  remoteApi,
  settingsApi,
  voiceApi,
  voiceUpdatePayload,
  type AutofillField,
  type AutofillKind,
  type BrowserVisionState,
  type HomeDeviceList,
  type DesktopSettings,
  type DesktopAppList,
  type HomeSettings,
  type VoiceUpdateBody,
} from '@/lib/api';
import {
  SettingsCard,
  SettingsRow,
  SettingsSection,
  StatusPill,
  Toggle,
} from './SettingsControls';
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
  RemotePairResult,
  RemoteStatus,
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

/** What a paired phone can do — and, just as importantly, what it cannot.
 *  Kept blunt: this is the copy someone reads before opening a port. */
const REMOTE_SUMMARY = [
  'See what Furi is working on, from your phone',
  'Approve, answer, pause or cancel work already under way',
  'Never starts anything — no chat, no shell, no new task',
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
    <SettingsCard
      icon={<Link2 size={15} />}
      title="Google account"
      summary={
        connected
          ? `Connected${status?.account_email ? ` as ${status.account_email}` : ''}`
          : connecting
            ? 'Waiting for you to finish in the browser…'
            : 'Powers email and calendar features'
      }
      accessory={
        <StatusPill tone={connected ? 'good' : connecting ? 'busy' : 'neutral'}>
          {connected ? 'connected' : connecting ? 'connecting' : 'off'}
        </StatusPill>
      }
    >
      <div className="space-y-3">
        <div className="space-y-1.5">
          {SCOPE_SUMMARY.map((line) => (
            <div key={line} className="flex items-center gap-2 text-xs text-slate-400">
              <ShieldCheck size={12} className="text-cyan-500/60 flex-shrink-0" />
              {line}
            </div>
          ))}
          <p className="text-[11px] text-slate-600 pt-1">
            Furi never deletes or relabels mail — those permissions are not requested.
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
    </SettingsCard>
  );
}

/** Local date+time, or an em dash. Device timestamps are UTC ISO from the
 *  backend (the `utc_iso` convention), so `new Date` reads them correctly. */
function shortWhen(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString();
}

/**
 * Remote access (Tier 2 item 5) — pair a phone that can approve, answer,
 * pause and cancel, and can start nothing.
 *
 * ⚠️ THIS CARD CANNOT TURN THE FEATURE ON, and that is deliberate rather than
 * an omission: `REMOTE_ENABLED` is a `.env` setting read at startup, because
 * opening a port is a decision that belongs with the process, not with a
 * toggle a page can flip. So the card reports the state and tells you how to
 * change it — the GoogleAccountCard's unconfigured-with-a-hint precedent.
 */
function RemoteAccessCard() {
  const [status, setStatus] = useState<RemoteStatus | null>(null);
  const [paired, setPaired] = useState<RemotePairResult | null>(null);
  const [name, setName] = useState('phone');
  const [error, setError] = useState<string | null>(null);
  const [isBusy, setIsBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setStatus(await remoteApi.status());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not reach the backend');
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handlePair = async () => {
    setIsBusy(true);
    setError(null);
    try {
      // ⚠️ The ONLY moment the token exists — hold the whole response in state
      // and never re-fetch it. Nothing on the backend can reissue it.
      setPaired(await remoteApi.pair(name.trim() || 'phone'));
      setCopied(false);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Pairing failed');
    } finally {
      setIsBusy(false);
    }
  };

  const handleRevoke = async (deviceId: string) => {
    setIsBusy(true);
    setError(null);
    try {
      await remoteApi.revoke(deviceId);
      // If the revoked device is the one whose token is on screen, that token
      // is now dead — stop showing a QR that cannot work.
      if (paired?.id === deviceId) setPaired(null);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Revoke failed');
    } finally {
      setIsBusy(false);
    }
  };

  const handleCopy = async () => {
    if (!paired) return;
    try {
      await navigator.clipboard.writeText(paired.url);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard can be refused; the URL is on screen and selectable anyway.
      setError('Could not copy — select the link and copy it by hand.');
    }
  };

  const enabled = status?.enabled ?? false;
  const devices = status?.devices ?? [];
  const liveCount = devices.filter((d) => d.live).length;
  const full = !!status && devices.filter((d) => d.live).length >= status.max_devices;

  return (
    <SettingsCard
      icon={<Smartphone size={15} />}
      title="Remote access"
      summary={
        enabled
          ? `Listening on ${status?.address}:${status?.port} · ${liveCount} device${liveCount === 1 ? '' : 's'} paired`
          : 'Approve work from your phone — currently off'
      }
      accessory={
        <StatusPill tone={enabled ? 'good' : 'neutral'}>{enabled ? 'on' : 'off'}</StatusPill>
      }
    >
      <div className="space-y-3">
        <div className="space-y-1.5">
          {REMOTE_SUMMARY.map((line) => (
            <div key={line} className="flex items-center gap-2 text-xs text-slate-400">
              <ShieldCheck size={12} className="text-cyan-500/60 flex-shrink-0" />
              {line}
            </div>
          ))}
          <p className="text-[11px] text-slate-600 pt-1">
            Those routes are not mounted on the remote port at all — they answer 404
            there whatever token is used. A paired phone also cannot pair another one.
          </p>
        </div>

        {!enabled && (
          <div className="p-2.5 rounded-lg bg-surface-2 border border-surface-border text-xs text-slate-400">
            Set <code className="text-cyan-400/90 font-mono">REMOTE_ENABLED=true</code> in{' '}
            <code className="text-cyan-400/90 font-mono">.env</code> and restart Furi to
            switch this on. Unlike the main API, it listens beyond this machine — use it
            only on a network you trust.
          </div>
        )}

        {error && (
          <div className="p-2.5 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs">
            {error}
          </div>
        )}

        {/* The one-time credential. Shown until this panel is left. */}
        {paired && (
          <div className="p-3 rounded-lg bg-cyan-500/5 border border-cyan-500/20 space-y-2.5">
            <div className="flex items-center gap-2 text-xs font-medium text-cyan-400">
              <QrCode size={13} />
              Scan this on the phone
            </div>
            {paired.qr ? (
              <img
                src={paired.qr}
                alt="Pairing QR code"
                className="w-40 h-40 bg-white rounded-lg p-1.5"
              />
            ) : (
              <p className="text-[11px] text-slate-500">
                No QR — install <code className="font-mono">segno</code> for one. The link
                below works on its own.
              </p>
            )}
            <div className="flex items-center gap-2">
              <code className="flex-1 min-w-0 truncate text-[11px] font-mono text-slate-300 bg-surface-2 border border-surface-border rounded px-2 py-1.5">
                {paired.url}
              </code>
              <button
                onClick={() => void handleCopy()}
                className="px-2.5 py-1.5 rounded-lg text-xs font-medium text-cyan-400 bg-cyan-500/10 border border-cyan-500/20 hover:bg-cyan-500/20 transition-colors flex-shrink-0"
              >
                {copied ? 'Copied' : 'Copy'}
              </button>
            </div>
            <p className="text-[11px] text-amber-400/90">
              Shown once. Only its fingerprint is stored, so this cannot be shown again —
              if you lose it, pair the device afresh.
            </p>
          </div>
        )}

        {/* Pair */}
        <div className="flex items-center gap-2">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={64}
            placeholder="Device name"
            disabled={!enabled}
            className="flex-1 min-w-0 px-2.5 py-1.5 rounded-lg text-xs bg-surface-2 border border-surface-border text-slate-300 placeholder:text-slate-600 focus:outline-none focus:border-cyan-500/30 disabled:opacity-40"
          />
          <button
            onClick={() => void handlePair()}
            disabled={!enabled || isBusy || full}
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium text-cyan-400 bg-cyan-500/10 border border-cyan-500/20 hover:bg-cyan-500/20 transition-colors disabled:opacity-40 flex-shrink-0"
            title={
              !enabled
                ? 'Set REMOTE_ENABLED=true in .env and restart first'
                : full
                  ? `That is the limit of ${status?.max_devices} paired devices — revoke one first`
                  : undefined
            }
          >
            {isBusy ? <Loader2 size={12} className="animate-spin" /> : <Plus size={12} />}
            Pair a device
          </button>
        </div>

        {/* Paired devices */}
        {devices.length > 0 && (
          <div className="space-y-1.5 pt-1">
            <p className="text-[10px] uppercase tracking-wide text-slate-600">
              Paired devices
            </p>
            {devices.map((d) => (
              <div
                key={d.id}
                className="flex items-center gap-2 px-2.5 py-2 rounded-lg bg-surface-2 border border-surface-border"
              >
                <div className="flex-1 min-w-0">
                  <p className="text-xs text-slate-300 truncate">{d.name}</p>
                  <p className="text-[10px] text-slate-600">
                    {d.live
                      ? `last seen ${shortWhen(d.last_seen_at)} · expires ${shortWhen(d.expires_at)}`
                      : d.revoked
                        ? 'revoked'
                        : 'expired'}
                  </p>
                </div>
                {d.live && (
                  <button
                    onClick={() => void handleRevoke(d.id)}
                    disabled={isBusy}
                    className="p-1.5 rounded-lg text-slate-500 hover:text-red-400 hover:bg-red-500/10 transition-colors disabled:opacity-40"
                    title="Revoke — takes effect on this device's next request"
                  >
                    <Trash2 size={12} />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </SettingsCard>
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
    <SettingsCard
      icon={<Sunrise size={15} />}
      accent="text-amber-400/80"
      title="Daily briefing"
      summary={
        enabled
          ? nextRun
            ? `Next: ${nextRun.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' })}`
            : `Every day at ${time}`
          : 'A morning summary of your day — off'
      }
      accessory={
        <Toggle
          checked={enabled}
          disabled={isBusy || !settings}
          onToggle={() => void save({ enabled: !enabled, time })}
          label="Daily briefing on/off"
        />
      }
    >
      <div className="space-y-3">
        <p className="text-xs text-slate-400">
          Each morning Furi gathers today's calendar, unread email, birthdays, and notes
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
    </SettingsCard>
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
    <SettingsCard
      icon={<Sparkles size={15} />}
      title="Initiative engine"
      summary={
        enabled
          ? nextRun
            ? `Next check: ${nextRun.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' })}`
            : `Checking every ${settings?.interval_minutes ?? 45} min`
          : 'Furi volunteers helpful suggestions — off'
      }
      accessory={
        <Toggle
          checked={enabled}
          disabled={isBusy || !settings}
          onToggle={() => void patch({ enabled: !enabled })}
          label="Initiative engine on/off"
        />
      }
    >
      {/* The description shows either way — an opened card that renders nothing
          reads as broken. Only the controls are gated on being enabled. */}
      <p className="text-xs text-slate-400">
        On a throttled schedule, Furi reviews your day (calendar, inbox, notes, and
        activity) and surfaces a few timely suggestions in the Suggestions panel.
      </p>

      {enabled && settings && (
        <div className="mt-3.5 space-y-3.5">
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
            During quiet hours Furi stays silent. Suggestions are also spaced at least{' '}
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
            When on, Furi periodically looks for genuinely useful things to raise — a
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
    </SettingsCard>
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
    <SettingsCard
      icon={<Database size={15} />}
      title="File search index"
      summary={
        enabled
          ? `${status?.indexed_files ?? 0} file(s) indexed`
          : 'Search your documents by meaning — off'
      }
      accessory={
        <Toggle
          checked={enabled}
          disabled={isBusy || !config}
          onToggle={() => void toggleEnabled()}
          label="File search index on/off"
        />
      }
    >
      <div className="space-y-4">
        <p className="text-xs text-slate-400">
          Furi indexes the text of your documents (.txt, .md, .pdf, .docx) in the folders
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
              Learned from your past file actions — Furi may suggest the top one when
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
    </SettingsCard>
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
          <StatusPill tone={status.device === 'cuda' ? 'on' : 'neutral'}>{deviceLabel}</StatusPill>
        )}
        <StatusPill
          tone={
            status.status === 'ready'
              ? 'good'
              : status.status === 'loading'
                ? 'busy'
                : status.status === 'error'
                  ? 'bad'
                  : 'neutral'
          }
        >
          {status.status === 'loading'
            ? percent !== null
              ? `downloading ${percent}%`
              : 'downloading…'
            : status.status === 'not_loaded'
              ? 'not loaded'
              : status.status}
        </StatusPill>
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

/** Display names for the recognition languages the backend offers. A code with
 *  no entry falls back to the code itself, so this can never gate the list. */
const LANGUAGE_LABELS: Record<string, string> = {
  en: 'English',
  ur: 'Urdu',
  hi: 'Hindi',
  ar: 'Arabic',
  es: 'Spanish',
  fr: 'French',
  de: 'German',
  it: 'Italian',
  pt: 'Portuguese',
  ru: 'Russian',
  tr: 'Turkish',
  zh: 'Chinese',
  ja: 'Japanese',
  ko: 'Korean',
};

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
          hint: 'Furi reads its answers aloud (also the header speaker toggle)',
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
          label: 'Wake word',
          hint: 'Always-on microphone; everything is processed on this machine and audio never leaves it',
          value: settings.wake_word,
        },
      ]
    : [];

  return (
    <SettingsCard
      icon={<Mic size={15} />}
      title="Voice"
      summary={
        enabled
          ? 'Push-to-talk and spoken replies — everything runs locally'
          : 'Talk to Furi and hear it answer — off'
      }
      accessory={
        <Toggle
          checked={enabled}
          disabled={isBusy || !settings}
          onToggle={() => void patch({ enabled: !enabled })}
          label="Voice on/off"
        />
      }
    >
      <div className="space-y-4">
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
              <Toggle
                checked={t.value}
                disabled={isBusy || !settings || !enabled}
                onToggle={() => void patch({ [t.key]: !t.value })}
                label={t.label}
              />
            </div>
          ))}
        </div>

        {/* Spoken approval — a CONSENT setting, so it gets its own block and
            says plainly what it changes. Default off. */}
        {enabled && (
          <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 px-3 py-2.5 space-y-2">
            <div className="flex flex-wrap items-center gap-3">
              <label className="text-xs font-medium text-amber-300/90" htmlFor="voice-spoken-approval">
                Approve out loud
              </label>
              <select
                id="voice-spoken-approval"
                value={settings?.spoken_approval ?? 'off'}
                disabled={isBusy || !settings}
                onChange={(e) => void patch({ spoken_approval: e.target.value })}
                className="px-2.5 py-1.5 rounded-lg text-xs bg-surface-2 border border-surface-border text-slate-200 disabled:opacity-40 focus:outline-none focus:border-amber-500/40"
              >
                <option value="off">Off — always use the card</option>
                <option value="write">Everything except deletes and sends</option>
                <option value="all">Everything, including deletes and sends</option>
              </select>
            </div>
            <p className="text-[11px] text-slate-400 leading-relaxed">
              Furi reads out exactly what it is about to do, and you can say
              “approve” instead of clicking. Your answer is tied to the steps you
              were read — if the plan changes, it asks again. A plain “yes” is not
              enough on purpose.
            </p>
          </div>
        )}

        {/* Wake word — the phrase and how it is heard. Only meaningful while
            the wake word is on, so it appears with it. */}
        {enabled && settings?.wake_word && (
          <div className="rounded-lg border border-surface-border bg-surface-2/40 px-3 py-2.5 space-y-2">
            <div className="flex flex-wrap items-center gap-3">
              <label className="text-xs font-medium text-slate-300" htmlFor="voice-wake-phrase">
                Wake phrase
              </label>
              <input
                id="voice-wake-phrase"
                type="text"
                defaultValue={settings.wake_phrase ?? 'furi'}
                disabled={isBusy || settings.wake_mode === 'model'}
                // On blur, not on every keystroke: each PUT re-validates and can
                // restart detection, and doing that per character would fight
                // the person typing.
                onBlur={(e) => {
                  const next = e.target.value.trim().toLowerCase();
                  if (next && next !== settings.wake_phrase) void patch({ wake_phrase: next });
                }}
                className="w-36 px-2.5 py-1.5 rounded-lg text-xs bg-surface-2 border border-surface-border text-slate-200 disabled:opacity-40 focus:outline-none focus:border-cyan-500/40"
              />
              <label className="text-xs text-slate-400" htmlFor="voice-wake-mode">
                Detected by
              </label>
              <select
                id="voice-wake-mode"
                value={settings.wake_mode ?? 'speech'}
                disabled={isBusy}
                onChange={(e) => void patch({ wake_mode: e.target.value })}
                className="px-2.5 py-1.5 rounded-lg text-xs bg-surface-2 border border-surface-border text-slate-200 disabled:opacity-40 focus:outline-none focus:border-cyan-500/40"
              >
                <option value="speech">Speech — any phrase you type</option>
                <option value="model">Model — “Hey Jarvis” only</option>
              </select>
            </div>
            <p className="text-[11px] text-slate-400 leading-relaxed">
              {settings.wake_mode === 'model'
                ? 'The bundled detector recognises “Hey Jarvis” and nothing else — the phrase is built into it. Switch to Speech to use your own.'
                : 'Furi notices when someone speaks, then checks locally whether they said your phrase. Any phrase works, and it costs a little more power than the fixed detector.'}
            </p>
          </div>
        )}

        {/* Recognition model + language + speaking voice + speed */}
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
          {/* ⚠️ PINNED, NOT DETECTED, and it is not a cosmetic preference:
              letting Whisper guess turned spoken English into Arabic script
              live, and guessing also costs a detection pass (measured: 343ms →
              264ms median once pinned). "Detect" is there for people who really
              do switch languages mid-conversation. */}
          <label className="text-xs text-slate-400" htmlFor="voice-stt-language">
            I speak
          </label>
          <select
            id="voice-stt-language"
            value={settings?.stt_language ?? 'en'}
            disabled={isBusy || !settings}
            onChange={(e) => void patch({ stt_language: e.target.value })}
            className="px-2.5 py-1.5 rounded-lg text-xs bg-surface-2 border border-surface-border text-slate-200 disabled:opacity-40 focus:outline-none focus:border-cyan-500/40"
          >
            {(settings?.stt_languages ?? ['en']).map((code) => (
              <option key={code} value={code}>
                {code === 'auto' ? 'Detect each time' : LANGUAGE_LABELS[code] ?? code}
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
              onClick={() => speakText("Hi — I'm Furi. This is how I sound.")}
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
    </SettingsCard>
  );
}

const PRESENCE_LABELS: Record<WorldModel['presence'], string> = {
  active: 'At the keyboard',
  idle: 'Idle',
  away: 'Away',
  unknown: 'Unknown',
};

/** Phase 8 — the Context Layer's privacy-first sensing controls + the
 *  "what Furi currently sees" audit surface. Every toggle PUTs immediately
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
    <SettingsCard
      icon={<Eye size={15} />}
      accent="text-emerald-400/80"
      title="Context & sensing"
      summary={
        enabled ? 'Sensing on — local only, nothing stored' : 'Off — Furi senses nothing'
      }
      accessory={
        <Toggle
          checked={enabled}
          disabled={!settings}
          onToggle={() => void updateSettings({ enabled: !enabled })}
          label="Master sensing switch"
        />
      }
    >
      <div className="space-y-3">
        <p className="text-xs text-slate-400">
          Lets Furi know what you're doing right now — presence, the active app, and
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
              <Toggle
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
              <Toggle
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
                    Let Furi use on-screen text to answer your questions (sends screen
                    text to the language model when you chat).
                  </p>
                </div>
                <Toggle
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
              <Toggle
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

            {/* Audit: what Furi currently sees */}
            <div className="mt-1 p-3 rounded-lg bg-surface-2/50 border border-surface-border space-y-1.5">
              <div className="flex items-center gap-2 text-[10px] uppercase tracking-wide text-slate-500">
                <ShieldCheck size={11} /> What Furi currently sees
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
    </SettingsCard>
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

function BrowserAccountCard() {
  const [loginOpen, setLoginOpen] = useState(false);
  const [isBusy, setIsBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const { login_open } = await browserApi.accountStatus();
      setLoginOpen(login_open);
    } catch {
      /* browser control is optional; a missing endpoint is not an error here */
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleSignIn = async () => {
    setIsBusy(true);
    setError(null);
    try {
      await browserApi.openLogin();
      setLoginOpen(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not open the sign-in window');
    } finally {
      setIsBusy(false);
    }
  };

  const handleDone = async () => {
    setIsBusy(true);
    try {
      await browserApi.closeLogin();
      setLoginOpen(false);
    } finally {
      setIsBusy(false);
    }
  };

  return (
    <SettingsCard
      icon={<Chrome size={15} />}
      title="Browser account"
      summary="Sign into YouTube/Google so Furi plays as you"
      accessory={
        <StatusPill tone={loginOpen ? 'busy' : 'neutral'}>
          {loginOpen ? 'window open' : 'ready'}
        </StatusPill>
      }
    >
      <div className="space-y-3">
        <p className="text-xs text-slate-400">
          Furi drives its own <span className="text-slate-300">Chrome</span> window with a
          private profile — separate from your everyday Chrome. Sign in once here and it stays
          signed in for every future play. You type your password directly into Google;
          Furi never sees or stores it.
        </p>
        <p className="text-[11px] text-slate-600">
          A public song plays fine without signing in — this only makes playback use your account
          (history, recommendations). Note: Google sometimes refuses sign-in in an automated
          window; if it does, that's Google's bot check, not a Furi error.
        </p>

        {error && (
          <div className="p-2.5 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs">
            {error}
          </div>
        )}

        <div className="flex items-center gap-2">
          {loginOpen ? (
            <>
              <button
                onClick={() => void handleDone()}
                disabled={isBusy}
                className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium text-slate-200 bg-cyan-500/15 border border-cyan-500/30 hover:bg-cyan-500/25 transition-colors disabled:opacity-40"
              >
                <CheckCircle2 size={12} />
                I've finished signing in
              </button>
              <span className="text-[11px] text-slate-500">
                Complete the sign-in in the open window, then click this.
              </span>
            </>
          ) : (
            <button
              onClick={() => void handleSignIn()}
              disabled={isBusy}
              className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium text-slate-200 bg-surface-2 border border-surface-border hover:border-cyan-500/30 transition-colors disabled:opacity-40"
            >
              {isBusy ? <Loader2 size={12} className="animate-spin" /> : <Chrome size={12} />}
              Sign in to browser
            </button>
          )}
        </div>
      </div>
    </SettingsCard>
  );
}

function BrowserVisionCard() {
  const [state, setState] = useState<BrowserVisionState | null>(null);
  const [isBusy, setIsBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        setState(await browserApi.getVision());
      } catch {
        /* browser control is optional; a missing endpoint is not an error here */
      }
    })();
  }, []);

  const handleToggle = async () => {
    if (!state || isBusy || !state.configured) return;
    const next = !state.enabled;
    // Optimistic + revert (the FileIndexCard lesson).
    setState({ ...state, enabled: next });
    setIsBusy(true);
    setError(null);
    try {
      setState(await browserApi.setVision(next));
    } catch (e) {
      setState({ ...state, enabled: !next });
      setError(e instanceof Error ? e.message : 'Could not update the setting');
    } finally {
      setIsBusy(false);
    }
  };

  const enabled = state?.enabled ?? false;
  const configured = state?.configured ?? false;

  return (
    <SettingsCard
      icon={<Eye size={15} />}
      title="Browser vision fallback"
      summary={
        !configured
          ? 'Needs an image-capable model configured in .env'
          : enabled
            ? 'On — used only when the page structure is not enough'
            : `Let Furi "look" at a page when text alone can't find a control`
      }
      accessory={
        <Toggle
          checked={enabled}
          disabled={isBusy || !configured}
          onToggle={() => void handleToggle()}
          label="Enable browser vision fallback"
        />
      }
    >
      <div className="space-y-3">
        <p className="text-xs text-slate-400">
          When driving a live site, Furi reads the page's structure to decide what to click.
          On pages with icon-only buttons or visual-only controls that can fail. With this on,
          it falls back to a <span className="text-slate-300">screenshot</span> sent to an
          image-capable model <span className="text-slate-300">only when it gets stuck</span> —
          the screenshot is held in memory and never saved. It still clicks a real page element,
          never blind coordinates, so every safety rule is unchanged.
        </p>

        {!configured && (
          <div className="p-2.5 rounded-lg bg-surface-2 border border-surface-border text-[11px] text-slate-500">
            No vision key is configured. Add <span className="font-mono text-slate-400">VISION_API_KEY</span>{' '}
            (or reuse <span className="font-mono text-slate-400">GEMINI_API_KEY</span>) in your{' '}
            <span className="font-mono text-slate-400">.env</span> to use this. Until then Furi
            stays text-only when driving the browser.
          </div>
        )}

        {configured && state && (
          <p className="text-[11px] text-slate-600">
            Using <span className="text-slate-500">{state.provider}</span> ·{' '}
            <span className="font-mono text-slate-500">{state.model}</span>. Off by default; each
            fallback sends one screenshot and is bounded per task.
          </p>
        )}

        {error && (
          <div className="p-2.5 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs">
            {error}
          </div>
        )}
      </div>
    </SettingsCard>
  );
}

const AUTOFILL_KINDS: { value: AutofillKind; label: string }[] = [
  { value: 'text', label: 'Text' },
  { value: 'link', label: 'Link' },
  { value: 'document', label: 'Document (file path)' },
  { value: 'secret', label: 'Secret (hidden)' },
];

/** The grounded autofill profile (Phase 15.2): the curated data Furi fills web
 *  forms from. A form value must trace to one of these fields or the user's own
 *  words — never a web page. Secrets are display-masked and never sent to the AI. */
function AutofillCard() {
  const [fields, setFields] = useState<AutofillField[]>([]);
  const [label, setLabel] = useState('');
  const [value, setValue] = useState('');
  const [kind, setKind] = useState<AutofillKind>('text');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setFields(await autofillApi.list());
    } catch {
      /* the profile is optional; a missing endpoint is not an error here */
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const add = async () => {
    if (!label.trim() || !value.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await autofillApi.upsert({ label: label.trim(), value: value.trim(), kind });
      setLabel('');
      setValue('');
      setKind('text');
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save the field');
    } finally {
      setBusy(false);
    }
  };

  const remove = async (key: string) => {
    try {
      await autofillApi.remove(key);
      await refresh();
    } catch {
      /* best-effort; the list re-syncs on the next load */
    }
  };

  const shown = (f: AutofillField): string =>
    f.is_secret ? '••••••••' : f.value ?? '';

  return (
    <SettingsCard
      icon={<ShieldCheck size={15} />}
      title="Autofill profile"
      summary={
        fields.length > 0
          ? `${fields.length} field${fields.length === 1 ? '' : 's'} saved`
          : 'Your data for filling web forms — grounded, never invented'
      }
      /* No accessory: the summary already states the count, and a bare "2"
         chip beside "2 fields saved" is the same fact twice. */
    >
      <div className="space-y-3">
        <p className="text-[11px] text-slate-500">
          When Furi fills a form on a live site, every value must come from this profile
          or your own words — never from the page. Secrets are hidden from the AI and only
          substituted at the moment of filling.
        </p>

        {fields.length > 0 && (
          <div className="space-y-1.5">
            {fields.map((f) => (
              <div
                key={f.id}
                className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg bg-surface-2 border border-surface-border"
              >
                <span className="text-xs font-medium text-slate-300 w-28 truncate">{f.label}</span>
                <span
                  className={clsx(
                    'flex-1 text-xs truncate',
                    f.is_secret ? 'text-slate-500 font-mono' : 'text-slate-400'
                  )}
                >
                  {shown(f)}
                </span>
                {f.is_secret && (
                  <span className="text-[9px] px-1.5 py-0.5 rounded-full bg-amber-500/10 text-amber-400 border border-amber-500/20">
                    secret
                  </span>
                )}
                {f.kind === 'document' && (
                  <span className="text-[9px] px-1.5 py-0.5 rounded-full bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
                    file
                  </span>
                )}
                <button
                  onClick={() => void remove(f.key)}
                  className="text-slate-600 hover:text-red-400 transition-colors"
                  title="Remove"
                >
                  <X size={13} />
                </button>
              </div>
            ))}
          </div>
        )}

        {error && (
          <div className="p-2.5 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs">
            {error}
          </div>
        )}

        {/* Add a field */}
        <div className="flex flex-wrap items-center gap-2">
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="Label (e.g. Full name)"
            className="flex-1 min-w-[8rem] px-2.5 py-1.5 rounded-lg bg-surface-2 border border-surface-border text-xs text-slate-200 placeholder:text-slate-600 focus:outline-none focus:border-cyan-500/40"
          />
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            type={kind === 'secret' ? 'password' : 'text'}
            placeholder={kind === 'document' ? 'File path' : 'Value'}
            className="flex-1 min-w-[8rem] px-2.5 py-1.5 rounded-lg bg-surface-2 border border-surface-border text-xs text-slate-200 placeholder:text-slate-600 focus:outline-none focus:border-cyan-500/40"
          />
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as AutofillKind)}
            className="px-2 py-1.5 rounded-lg bg-surface-2 border border-surface-border text-xs text-slate-300 focus:outline-none focus:border-cyan-500/40"
          >
            {AUTOFILL_KINDS.map((k) => (
              <option key={k.value} value={k.value}>
                {k.label}
              </option>
            ))}
          </select>
          <button
            onClick={() => void add()}
            disabled={busy || !label.trim() || !value.trim()}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium text-slate-200 bg-cyan-500/15 border border-cyan-500/30 hover:bg-cyan-500/25 transition-colors disabled:opacity-40"
          >
            {busy ? <Loader2 size={12} className="animate-spin" /> : <Plus size={12} />}
            Add
          </button>
        </div>
      </div>
    </SettingsCard>
  );
}

/**
 * Home & IoT (Feature 1) — the Home Assistant connection plus the audit list
 * of what Furi can see and control.
 *
 * The device list is the TRUST SURFACE, and it is why this card shows one at
 * all: a feature that can unlock a door has to be able to answer "what exactly
 * can it reach?" without the user running a plan to find out. It is read-only —
 * the agent path goes through the tools and their approval gate, never here.
 */
/** One capability sub-toggle — now just the shared row plus the shared switch,
 *  so it cannot drift away from every other toggle in the panel. */
function SubToggle({
  label,
  hint,
  checked,
  disabled,
  onToggle,
}: {
  label: string;
  hint: string;
  checked: boolean;
  disabled: boolean;
  onToggle: () => void;
}) {
  return (
    <SettingsRow
      label={label}
      hint={hint}
      control={
        <Toggle checked={checked} disabled={disabled} onToggle={onToggle} label={label} />
      }
    />
  );
}

function DesktopControlCard() {
  const [settings, setSettings] = useState<DesktopSettings | null>(null);
  const [apps, setApps] = useState<DesktopAppList | null>(null);
  const [showApps, setShowApps] = useState(false);
  const [isBusy, setIsBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setSettings(await desktopApi.getSettings());
    } catch {
      /* optional feature — a missing endpoint is not an error */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const loadApps = useCallback(async () => {
    try {
      setApps(await desktopApi.listApps());
    } catch {
      /* the audit list is best-effort */
    }
  }, []);

  const save = async (update: Partial<DesktopSettings>) => {
    if (!settings) return;
    setIsBusy(true);
    setError(null);
    // Optimistic + revert (the FileIndexCard lesson): every toggle writes
    // THROUGH immediately rather than waiting for a separate Save click.
    const optimistic = { ...settings, ...update };
    setSettings(optimistic);
    try {
      setSettings(
        await desktopApi.updateSettings({
          enabled: optimistic.enabled,
          allow_launch: optimistic.allow_launch,
          allow_close: optimistic.allow_close,
          allow_input: optimistic.allow_input,
          allow_clipboard: optimistic.allow_clipboard,
          allow_screenshot: optimistic.allow_screenshot,
          screenshot_retention_days: optimistic.screenshot_retention_days,
        }),
      );
    } catch (e) {
      setSettings(settings); // revert
      setError(e instanceof Error ? e.message : 'Could not save the desktop settings');
    } finally {
      setIsBusy(false);
    }
  };

  if (!settings) return null;
  const off = !settings.enabled;

  return (
    <SettingsCard
      icon={<Monitor size={15} />}
      title="Desktop control"
      summary={
        !settings.supported
          ? 'Not available on this platform'
          : settings.enabled
            ? 'On — windows, apps, volume and the clipboard'
            : 'Windows, apps, volume and the clipboard — off'
      }
      accessory={
        <Toggle
          checked={settings.enabled}
          disabled={isBusy || !settings.supported}
          onToggle={() => void save({ enabled: !settings.enabled })}
          label="Desktop control on/off"
        />
      }
    >
      <div className="space-y-3">
        <p className="text-xs text-slate-400">
          Furi already sees which window you have open. This lets it act: switch to a
          window, open an app, turn the volume down. Every action is shown to you for
          approval first, naming the exact window or application.
        </p>

        {!settings.supported && (
          <div className="text-[11px] text-amber-400/80 bg-amber-500/5 border border-amber-500/20 rounded-lg px-2.5 py-2">
            {settings.detail || 'Desktop control is not supported on this platform.'}
          </div>
        )}

        {/* The master switch lives in the card header (reachable without
            opening the card); this section is only what it may then do. */}
        <div className="pt-1 space-y-2.5 border-t border-surface-border/60">
          <p className="text-[10px] uppercase tracking-wide text-slate-600 pt-2.5">
            What it may do
          </p>
          <SubToggle
            label="Open applications"
            hint="Only apps in your Start Menu — never a path or a command."
            checked={settings.allow_launch}
            disabled={isBusy || off}
            onToggle={() => void save({ allow_launch: !settings.allow_launch })}
          />
          <SubToggle
            label="Volume and media keys"
            hint="Set the volume, mute, play/pause, skip."
            checked={settings.allow_input}
            disabled={isBusy || off}
            onToggle={() => void save({ allow_input: !settings.allow_input })}
          />
          <SubToggle
            label="Close windows"
            hint="Sends the same request as clicking the X — an app with unsaved work still asks you."
            checked={settings.allow_close}
            disabled={isBusy || off}
            onToggle={() => void save({ allow_close: !settings.allow_close })}
          />
          <SubToggle
            label="Clipboard"
            hint="Read and replace it. Your clipboard may hold a password you just copied."
            checked={settings.allow_clipboard}
            disabled={isBusy || off}
            onToggle={() => void save({ allow_clipboard: !settings.allow_clipboard })}
          />
          <SubToggle
            label="Screenshots"
            hint="Saves an image of every display to ~/.jarvis/screenshots. Furi does not look at it."
            checked={settings.allow_screenshot}
            disabled={isBusy || off}
            onToggle={() => void save({ allow_screenshot: !settings.allow_screenshot })}
          />
        </div>

        {settings.allow_screenshot && (
          <label className="flex items-center justify-between gap-3">
            <span className="text-[11px] text-slate-500">Delete screenshots after</span>
            <span className="flex items-center gap-1.5">
              <input
                type="number"
                min={1}
                max={365}
                value={settings.screenshot_retention_days}
                onChange={(e) =>
                  void save({ screenshot_retention_days: Number(e.target.value) || 7 })
                }
                disabled={isBusy}
                className="w-16 px-2 py-1 rounded-lg bg-surface-2 border border-surface-border text-xs text-slate-200 font-mono focus:outline-none focus:border-cyan-500/40"
              />
              <span className="text-[11px] text-slate-500">days</span>
            </span>
          </label>
        )}

        {/* The trust surface: launch_app takes a NAME and resolves it against
            this registry — it has no path or command parameter, so this list is
            the complete set of things it can start. */}
        <div className="pt-1 border-t border-surface-border/60">
          <button
            onClick={() => {
              setShowApps((v) => !v);
              if (!apps) void loadApps();
            }}
            className="w-full flex items-center justify-between text-[11px] text-slate-500 hover:text-slate-300 transition-colors pt-2.5"
          >
            <span>Applications Furi can open{apps ? ` (${apps.count})` : ''}</span>
            <span className="font-mono">{showApps ? '−' : '+'}</span>
          </button>
          {showApps && (
            <div className="mt-2 max-h-40 overflow-y-auto rounded-lg bg-surface-2 border border-surface-border px-2.5 py-2">
              {apps === null ? (
                <p className="text-[11px] text-slate-600">Reading the Start Menu…</p>
              ) : apps.count === 0 ? (
                <p className="text-[11px] text-slate-600">
                  {apps.detail || 'No applications found in the Start Menu.'}
                </p>
              ) : (
                <ul className="space-y-0.5">
                  {apps.apps.map((name) => (
                    <li key={name} className="text-[11px] text-slate-400 font-mono truncate">
                      {name}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>

        {error && <p className="text-[11px] text-red-400/80">{error}</p>}
      </div>
    </SettingsCard>
  );
}

function HomeCard() {
  const [settings, setSettings] = useState<HomeSettings | null>(null);
  const [devices, setDevices] = useState<HomeDeviceList | null>(null);
  const [baseUrl, setBaseUrl] = useState('');
  const [token, setToken] = useState('');
  const [isBusy, setIsBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [probe, setProbe] = useState<string | null>(null);
  const [showDevices, setShowDevices] = useState(false);

  const load = useCallback(async () => {
    try {
      const next = await homeApi.getSettings();
      setSettings(next);
      setBaseUrl(next.base_url);
    } catch {
      /* the home integration is optional — a missing endpoint is not an error */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async (update: Partial<HomeSettings> & { token?: string }) => {
    if (!settings) return;
    setIsBusy(true);
    setError(null);
    setProbe(null);
    // Optimistic + revert (the FileIndexCard lesson): the toggle writes
    // THROUGH immediately rather than waiting for a separate Save click, which
    // is what left an earlier card's "on" silently unpersisted.
    const optimistic = { ...settings, ...update } as HomeSettings;
    setSettings(optimistic);
    try {
      setSettings(
        await homeApi.updateSettings({
          enabled: optimistic.enabled,
          base_url: update.base_url ?? baseUrl,
          ...(update.token !== undefined ? { token: update.token } : {}),
        }),
      );
      setToken('');
    } catch (e) {
      setSettings(settings); // revert
      setError(e instanceof Error ? e.message : 'Could not save the home settings');
    } finally {
      setIsBusy(false);
    }
  };

  const handleTest = async () => {
    setIsBusy(true);
    setError(null);
    try {
      const result = await homeApi.testConnection();
      setProbe(result.detail);
      if (result.connected) void loadDevices();
    } catch (e) {
      setProbe(e instanceof Error ? e.message : 'Could not reach the hub');
    } finally {
      setIsBusy(false);
    }
  };

  const loadDevices = useCallback(async () => {
    try {
      setDevices(await homeApi.listDevices());
    } catch {
      /* the list is a convenience; a failure just leaves it empty */
    }
  }, []);

  if (!settings) return null;

  const connected = settings.enabled && settings.configured;

  return (
    <SettingsCard
      icon={<Home size={15} />}
      title="Home &amp; devices"
      summary={
        connected
          ? `Connected${devices ? ` · ${devices.devices.length} device(s)` : ''}`
          : settings.enabled
            ? 'Needs a hub address and token'
            : 'Lights, locks, blinds and heating — off'
      }
      accessory={
        <Toggle
          checked={settings.enabled}
          disabled={isBusy || (!settings.enabled && !baseUrl.trim())}
          onToggle={() => void save({ enabled: !settings.enabled })}
          label="Home control on/off"
        />
      }
    >
      <div className="space-y-3">
        <p className="text-xs text-slate-400">
          Connect your Home Assistant hub and Furi can read and control the devices in
          your home. Every change — a light, a lock, the thermostat — is shown to you for
          approval first, naming the exact device and room.
        </p>

        <div className="space-y-2">
          <label className="block">
            <span className="text-[11px] text-slate-500">Hub address</span>
            <input
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              onBlur={() => baseUrl !== settings.base_url && void save({ base_url: baseUrl })}
              placeholder="http://homeassistant.local:8123"
              className="w-full mt-1 px-2.5 py-1.5 rounded-lg bg-surface-2 border border-surface-border text-xs text-slate-200 font-mono placeholder:text-slate-600 focus:outline-none focus:border-cyan-500/40"
            />
          </label>

          <label className="block">
            <span className="text-[11px] text-slate-500">
              Long-lived access token{' '}
              {settings.has_token && <span className="text-cyan-500/70">— one is stored</span>}
            </span>
            <div className="flex gap-2 mt-1">
              <input
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder={settings.has_token ? '••••••••  (leave blank to keep)' : 'Paste your token'}
                className="flex-1 px-2.5 py-1.5 rounded-lg bg-surface-2 border border-surface-border text-xs text-slate-200 font-mono placeholder:text-slate-600 focus:outline-none focus:border-cyan-500/40"
              />
              <button
                onClick={() => void save({ token })}
                disabled={isBusy || !token.trim()}
                className="px-3 py-1.5 rounded-lg text-xs font-medium text-slate-200 bg-surface-2 border border-surface-border hover:border-cyan-500/30 transition-colors disabled:opacity-40"
              >
                Save
              </button>
            </div>
            <span className="text-[11px] text-slate-600">
              Home Assistant → your profile → Long-lived access tokens → Create token.
              It is stored in <code className="text-slate-500">~/.jarvis</code>, never in the
              database.
            </span>
          </label>
        </div>

        {error && (
          <div className="p-2.5 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs">
            {error}
          </div>
        )}
        {probe && (
          <div className="p-2.5 rounded-lg bg-surface-2 border border-surface-border text-xs text-slate-400">
            {probe}
          </div>
        )}

        <div className="flex items-center gap-2">
          <button
            onClick={() => void handleTest()}
            disabled={isBusy || !settings.configured}
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium text-slate-200 bg-surface-2 border border-surface-border hover:border-cyan-500/30 transition-colors disabled:opacity-40"
          >
            {isBusy ? <Loader2 size={12} className="animate-spin" /> : <Link2 size={12} />}
            Test connection
          </button>
          <button
            onClick={() => {
              setShowDevices((v) => !v);
              if (!devices) void loadDevices();
            }}
            disabled={!settings.configured}
            className="px-3 py-1.5 rounded-lg text-xs font-medium text-slate-400 bg-surface-2 border border-surface-border hover:border-cyan-500/30 transition-colors disabled:opacity-40"
          >
            {showDevices ? 'Hide devices' : 'What can Furi see?'}
          </button>
        </div>

        {showDevices && devices && (
          <div className="rounded-lg border border-surface-border bg-surface-2/50 max-h-56 overflow-y-auto">
            {devices.devices.length === 0 ? (
              <p className="px-3 py-2 text-[11px] text-slate-500">
                {devices.detail || 'No devices found on the hub.'}
              </p>
            ) : (
              <>
                <p className="px-3 pt-2 text-[10px] uppercase tracking-wide text-slate-600">
                  {devices.count} device(s) Furi can see
                </p>
                <ul className="px-3 py-2 space-y-1">
                  {devices.devices.map((d) => (
                    <li key={d.entity_id} className="flex items-center gap-2 text-[11px]">
                      <span className="text-slate-300 truncate flex-1">{d.name}</span>
                      {d.area && <span className="text-slate-600">{d.area}</span>}
                      <span className="text-slate-500 font-mono">{d.state}</span>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        )}
      </div>
    </SettingsCard>
  );
}

/**
 * Every card is COLLAPSED by default, which is the whole answer to the panel
 * feeling cluttered: twelve features used to render every control they own at
 * once, so opening Settings to change one thing meant scrolling past a hundred
 * others. Now it reads as a list — one line per feature, saying what state it
 * is in — and the one you came for opens on a click.
 */
export function SettingsPanel() {
  return (
    <div className="flex h-full flex-col overflow-hidden bg-surface">
      {/* ── Header ── */}
      <div className="flex-shrink-0 border-b border-surface-border bg-surface-1/50 px-6 py-4">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl border border-cyan-500/20 bg-gradient-to-br from-cyan-500/20 to-blue-600/20">
            <SettingsIcon size={16} className="text-cyan-400" />
          </div>
          <div>
            <h1 className="text-base font-semibold text-slate-200">Settings</h1>
            <p className="text-xs text-muted">
              Everything is off until you turn it on — open a card to see what it does
            </p>
          </div>
        </div>
      </div>

      {/* ── Body ── */}
      <div className="flex-1 overflow-y-auto px-6 py-5">
        <div className="max-w-2xl space-y-6 pb-8">
          <SettingsSection label="Integrations">
            <GoogleAccountCard />
            <BrowserAccountCard />
            <BrowserVisionCard />
            <AutofillCard />
          </SettingsSection>

          <SettingsSection label="This machine">
            <DesktopControlCard />
            <FileIndexCard />
            <ContextSensingCard />
          </SettingsSection>

          <SettingsSection label="Home">
            <HomeCard />
          </SettingsSection>

          <SettingsSection label="Voice">
            <VoiceCard />
          </SettingsSection>

          <SettingsSection label="Proactive">
            <DailyBriefingCard />
            <InitiativeCard />
          </SettingsSection>

          <SettingsSection label="Remote">
            <RemoteAccessCard />
          </SettingsSection>
        </div>
      </div>
    </div>
  );
}
