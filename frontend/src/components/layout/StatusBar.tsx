/**
 * Jarvis OS — Status Bar
 * Bottom bar showing backend connection, active model, and app version.
 */
import { useEffect } from 'react';
import { clsx } from 'clsx';
import { Circle, Cpu, Eye, Wifi, WifiOff, RefreshCw } from 'lucide-react';
import { useUIStore } from '@/stores/uiStore';
import { usePushStore } from '@/stores/pushStore';
import { useContextStore } from '@/stores/contextStore';

const SENSING_POLL_MS = 10_000;

export function StatusBar() {
  const { backendStatus, healthData, isCheckingHealth, checkBackendHealth } = useUIStore();
  const pushConnected = usePushStore((s) => s.connected);
  const sensing = useContextStore((s) => s.status);
  const fetchSensingStatus = useContextStore((s) => s.fetchStatus);

  // The required visible "sensing on" indicator — poll the cheap status.
  useEffect(() => {
    void fetchSensingStatus();
    const t = setInterval(() => void fetchSensingStatus(), SENSING_POLL_MS);
    return () => clearInterval(t);
  }, [fetchSensingStatus]);

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
