/**
 * Jarvis OS — Status Bar
 * Bottom bar showing backend connection, active model, and app version.
 */
import { useEffect, useState } from 'react';
import { clsx } from 'clsx';
import { Circle, Cpu, Eye, Mic, Play, Sparkles, Wifi, WifiOff, RefreshCw, X } from 'lucide-react';
import { useUIStore } from '@/stores/uiStore';
import { usePushStore } from '@/stores/pushStore';
import { useContextStore } from '@/stores/contextStore';
import { useVoiceStore } from '@/stores/voiceStore';
import { useBrowserStore } from '@/stores/browserStore';
import { initiativeApi } from '@/lib/api';

const SENSING_POLL_MS = 10_000;
const INITIATIVE_POLL_MS = 30_000;

export function StatusBar() {
  const { backendStatus, healthData, isCheckingHealth, checkBackendHealth } = useUIStore();
  const pushConnected = usePushStore((s) => s.connected);
  const sensing = useContextStore((s) => s.status);
  const fetchSensingStatus = useContextStore((s) => s.fetchStatus);
  // Phase 12.2: the required visible indicator while the wake-word mic is armed.
  const wakeWordOn = useVoiceStore(
    (s) => !!s.settings?.enabled && !!s.settings?.wake_word
  );
  const [initiativeOn, setInitiativeOn] = useState(false);
  // Phase 14: the "▶ Playing" indicator + stop control for a browse window.
  const media = useBrowserStore((s) => ({ playing: s.playing, title: s.title, stopping: s.stopping }));
  const stopMedia = useBrowserStore((s) => s.stop);

  // The required visible "sensing on" indicator — poll the cheap status.
  useEffect(() => {
    void fetchSensingStatus();
    const t = setInterval(() => void fetchSensingStatus(), SENSING_POLL_MS);
    return () => clearInterval(t);
  }, [fetchSensingStatus]);

  // Phase 9: a quiet "Jarvis may speak first" indicator. Proactivity state
  // changes rarely, so a slow poll is plenty; failures keep the last value.
  useEffect(() => {
    let alive = true;
    const check = () =>
      initiativeApi
        .getSettings()
        .then((s) => alive && setInitiativeOn(s.enabled))
        .catch(() => {});
    void check();
    const t = setInterval(() => void check(), INITIATIVE_POLL_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  const isConnected = backendStatus === 'ok';
  const isDegraded = backendStatus === 'degraded';

  return (
    <footer className="h-7 flex items-center justify-between px-4 bg-surface-1 border-t border-surface-border text-[11px] font-mono flex-shrink-0">
      {/* Left: Connection status */}
      <div className="flex items-center gap-3">
        <button
          onClick={() => checkBackendHealth()}
          className="flex items-center gap-1.5 text-muted hover:text-slate-400 transition-fast group"
          title="Check backend status"
        >
          {isCheckingHealth ? (
            <RefreshCw size={10} className="animate-spin text-cyan-500" />
          ) : isConnected ? (
            <Wifi size={10} className="text-emerald-500" />
          ) : (
            <WifiOff size={10} className="text-danger" />
          )}
          <span
            className={clsx(
              isConnected
                ? 'text-emerald-500'
                : isDegraded
                ? 'text-warning'
                : 'text-danger'
            )}
          >
            {isCheckingHealth
              ? 'Connecting...'
              : isConnected
              ? 'Backend: Connected'
              : isDegraded
              ? 'Backend: Degraded'
              : 'Backend: Offline'}
          </span>
        </button>

        {/* Qdrant status */}
        {healthData?.components.qdrant && (
          <div className="flex items-center gap-1">
            <Circle
              size={6}
              className={clsx(
                'fill-current',
                healthData.components.qdrant.status === 'ok'
                  ? 'text-emerald-500'
                  : 'text-warning'
              )}
            />
            <span className="text-muted">
              Qdrant:{' '}
              {healthData.components.qdrant.status === 'ok' ? 'Ready' : 'Offline'}
            </span>
          </div>
        )}

        {/* Push channel status (Phase 4) */}
        <div className="flex items-center gap-1" title="Server→client push channel">
          <Circle
            size={6}
            className={clsx(
              'fill-current',
              pushConnected ? 'text-emerald-500' : 'text-muted'
            )}
          />
          <span className="text-muted">
            Push: {pushConnected ? 'Live' : 'Off'}
          </span>
        </div>

        {/* Sensing indicator (Phase 8) — visible whenever the Context Layer is on */}
        {sensing?.enabled && (
          <div
            className="flex items-center gap-1"
            title={
              sensing.screen_ocr
                ? 'Context sensing on (device + screen) — local only'
                : 'Context sensing on (device) — local only'
            }
          >
            <Eye
              size={10}
              className={clsx(sensing.screen_ocr ? 'text-amber-400' : 'text-emerald-500')}
            />
            <span className={clsx(sensing.screen_ocr ? 'text-amber-400' : 'text-emerald-500')}>
              Sensing: On{sensing.screen_ocr ? ' (screen)' : ''}
            </span>
          </div>
        )}

        {/* Wake-word indicator (Phase 12.2) — the always-on mic must be visible */}
        {wakeWordOn && (
          <div
            className="flex items-center gap-1"
            title="Wake word on — listening for “Hey Jarvis” (on-device; audio never leaves this machine)"
          >
            <Mic size={10} className="text-amber-400" />
            <span className="text-amber-400">Listening · Hey Jarvis</span>
          </div>
        )}

        {/* Initiative indicator (Phase 9) — Jarvis may volunteer suggestions */}
        {initiativeOn && (
          <div className="flex items-center gap-1" title="Initiative engine on — Jarvis may suggest things proactively">
            <Sparkles size={10} className="text-cyan-400" />
            <span className="text-cyan-400">Initiative: On</span>
          </div>
        )}

        {/* Browser playback (Phase 14) — a browse window is open and playing */}
        {media.playing && (
          <div className="flex items-center gap-1" title={media.title || 'Playing in the browser'}>
            <Play size={10} className="text-emerald-500 fill-current" />
            <span className="text-emerald-500 max-w-[220px] truncate">
              Playing{media.title ? `: ${media.title}` : ''}
            </span>
            <button
              onClick={() => void stopMedia()}
              disabled={media.stopping}
              className="ml-0.5 text-muted hover:text-danger transition-fast disabled:opacity-50"
              title="Stop playback"
            >
              <X size={11} />
            </button>
          </div>
        )}
      </div>

      {/* Center: Model info */}
      <div className="flex items-center gap-2 text-muted">
        {healthData && (
          <>
            <Cpu size={10} className="text-cyan-500/60" />
            <span className="text-cyan-400/70">
              {healthData.provider}/{healthData.model}
            </span>
          </>
        )}
      </div>

      {/* Right: App version */}
      <div className="flex items-center gap-2 text-muted">
        <span>Jarvis OS</span>
        <span className="text-surface-border">|</span>
        <span className="text-cyan-500/50">v{healthData?.version ?? '0.1.0'}</span>
      </div>
    </footer>
  );
}
