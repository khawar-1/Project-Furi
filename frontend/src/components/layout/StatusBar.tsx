/**
 * Jarvis OS — Status Bar
 * Bottom bar showing backend connection, active model, and app version.
 */
import { useEffect, useState } from 'react';
import { clsx } from 'clsx';
import { Bot, Circle, Cpu, Eye, FileCheck, Mic, Play, Sparkles, Wifi, WifiOff, RefreshCw, X } from 'lucide-react';
import { useUIStore } from '@/stores/uiStore';
import { usePushStore } from '@/stores/pushStore';
import { useContextStore } from '@/stores/contextStore';
import { useVoiceStore } from '@/stores/voiceStore';
import { useBrowserStore } from '@/stores/browserStore';
import { useTasksStore } from '@/stores/tasksStore';
import { initiativeApi } from '@/lib/api';

const SENSING_POLL_MS = 10_000;
const INITIATIVE_POLL_MS = 30_000;
const AGENTS_POLL_MS = 8_000;
const AGENT_ACTIVE = new Set([
  'running', 'awaiting_approval', 'awaiting_choice',
  // Paused by the user and holding — still an agent waiting on you, so it must
  // stay visible in the indicator; losing it there would hide the one state
  // that needs a reply to move.
  'paused',
]);

export function StatusBar() {
  const { backendStatus, healthData, isCheckingHealth, checkBackendHealth } = useUIStore();
  const setActivePanel = useUIStore((s) => s.setActivePanel);
  const pushConnected = usePushStore((s) => s.connected);
  // Active background agents (workers) — clickable jump to the Agents panel.
  const agentActive = useTasksStore(
    (s) => s.tasks.filter((t) => AGENT_ACTIVE.has(t.status)).length
  );
  const loadTasks = useTasksStore((s) => s.loadTasks);
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
  // Phase 14.6: a commit result window left open so the user can see the response.
  const resultWindow = useBrowserStore((s) => ({
    open: s.windowOpen,
    title: s.windowTitle,
    closing: s.closingWindow,
  }));
  const closeWindow = useBrowserStore((s) => s.closeWindow);
  // 2026-08-01: several browser tasks can be open at once, one tab each.
  const tabs = useBrowserStore((s) => s.tabs);
  const [tabsOpen, setTabsOpen] = useState(false);

  // The required visible "sensing on" indicator — poll the cheap status.
  useEffect(() => {
    void fetchSensingStatus();
    const t = setInterval(() => void fetchSensingStatus(), SENSING_POLL_MS);
    return () => clearInterval(t);
  }, [fetchSensingStatus]);

  // Keep the active-agents count fresh even when the Agents panel is closed —
  // a just-started worker does not push until it pauses or finishes. Silent,
  // low-frequency; the "task" push reconciles between polls.
  useEffect(() => {
    void loadTasks({ silent: true });
    const t = setInterval(() => void loadTasks({ silent: true }), AGENTS_POLL_MS);
    return () => clearInterval(t);
  }, [loadTasks]);

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

        {/* Active background agents (workers) — click to open the Agents panel */}
        {agentActive > 0 && (
          <button
            onClick={() => setActivePanel('agents')}
            className="flex items-center gap-1 text-cyan-400 hover:text-cyan-300 transition-fast"
            title={`${agentActive} agent${agentActive === 1 ? '' : 's'} working in the background`}
          >
            <Bot size={10} className="text-cyan-400" />
            <span>
              {agentActive} agent{agentActive === 1 ? '' : 's'} working
            </span>
          </button>
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

        {/* Kept-open browser window — a submitted form's response page (14.6) or
            the agent's own tabs (2026-08-01), left open so the user can see the
            result. Several browser tasks can be open at once, so past one tab
            this becomes a count with a per-tab list. */}
        {resultWindow.open && (
          <div className="relative flex items-center gap-1">
            <FileCheck size={10} className="text-cyan-400" />
            {tabs.length > 1 ? (
              <>
                <button
                  onClick={() => setTabsOpen((v) => !v)}
                  className="text-cyan-400 hover:text-cyan-300 transition-fast"
                  title="Show the open browser tabs"
                >
                  {tabs.length} browser tabs
                </button>
                {tabsOpen && (
                  <div className="absolute bottom-full left-0 mb-1 z-50 w-72 rounded-md border border-border bg-surface shadow-lg py-1">
                    {tabs.map((tab) => (
                      <div
                        key={tab.site || tab.url}
                        className="flex items-center gap-1 px-2 py-1 hover:bg-white/5"
                      >
                        <span
                          className="flex-1 truncate text-cyan-400"
                          title={tab.goal || tab.url}
                        >
                          {tab.title || tab.site || tab.url}
                        </span>
                        {tab.busy && (
                          <span className="text-[10px] text-amber-400" title="Mid-task — waiting on you or still working">
                            busy
                          </span>
                        )}
                        <button
                          onClick={() => void closeWindow(tab.site)}
                          disabled={resultWindow.closing}
                          className="text-muted hover:text-danger transition-fast disabled:opacity-50"
                          title="Close this tab"
                        >
                          <X size={11} />
                        </button>
                      </div>
                    ))}
                    <button
                      onClick={() => {
                        setTabsOpen(false);
                        void closeWindow();
                      }}
                      disabled={resultWindow.closing}
                      className="mt-1 w-full border-t border-border px-2 pt-1 text-left text-muted hover:text-danger transition-fast disabled:opacity-50"
                    >
                      Close all tabs
                    </button>
                  </div>
                )}
              </>
            ) : (
              <>
                <span
                  className="text-cyan-400 max-w-[220px] truncate"
                  title={resultWindow.title || 'A browser window is open'}
                >
                  Browser window{resultWindow.title ? `: ${resultWindow.title}` : ' open'}
                </span>
                <button
                  onClick={() => void closeWindow()}
                  disabled={resultWindow.closing}
                  className="ml-0.5 text-muted hover:text-danger transition-fast disabled:opacity-50"
                  title="Close the browser window"
                >
                  <X size={11} />
                </button>
              </>
            )}
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
