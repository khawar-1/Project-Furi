/**
 * Jarvis OS — Status Bar
 * Bottom bar showing backend connection, active model, and app version.
 */
import { clsx } from 'clsx';
import { Circle, Cpu, Wifi, WifiOff, RefreshCw } from 'lucide-react';
import { useUIStore } from '@/stores/uiStore';

export function StatusBar() {
  const { backendStatus, healthData, isCheckingHealth, checkBackendHealth } = useUIStore();

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
