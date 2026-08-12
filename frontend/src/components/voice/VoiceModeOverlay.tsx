/**
 * Furi OS — Voice Mode overlay
 *
 * Covers the chat's message area with the sphere while a hands-free
 * conversation runs. The header, the sidebar and the status bar stay live, so
 * the app never feels hijacked — only the reading surface is handed over.
 *
 * ⚠️ THE CHAT IS HIDDEN, NEVER DESTROYED. The message list stays mounted
 * underneath and is merely covered, so scroll position survives and leaving is
 * instant. Every voice turn is an ordinary chatStore message, so exiting
 * reveals the conversation continued rather than replayed.
 */
import { useEffect, useMemo, useState } from 'react';
import { clsx } from 'clsx';
import { useShallow } from 'zustand/react/shallow';
import { useChatStore } from '@/stores/chatStore';
import { useVoiceStore } from '@/stores/voiceStore';
import { exitVoiceMode, toggleVoiceListening } from '@/lib/voiceModeControl';
import { VoiceOrb, type OrbState } from './VoiceOrb';
import { VoiceOutputPanel, currentTurnKey, turnOutputKind } from './VoiceOutputPanel';

/**
 * `speaking` drops to false whenever the playback queue momentarily drains
 * between two sentences (voiceOutput's pumpPlayback) — voiceConversation.ts
 * debounces the same edge at 700ms for the same reason. Without this the orb
 * strobes between "speaking" and "thinking" mid-reply.
 */
const SPEAKING_SETTLE_MS = 600;

/** True the moment `value` is true; false only after it has been false for
 *  `ms` without interruption. */
function useSettledFlag(value: boolean, ms: number): boolean {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    if (value) {
      setSettled(true);
      return;
    }
    const timer = window.setTimeout(() => setSettled(false), ms);
    return () => window.clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

export function VoiceModeOverlay() {
  const { phase, speaking, interimText, voiceError } = useVoiceStore(
    useShallow((s) => ({
      phase: s.phase,
      speaking: s.speaking,
      interimText: s.interimText,
      voiceError: s.error,
    }))
  );
  // Primitive selectors: the store fires on every streamed delta, and Zustand
  // compares the RESULT — so the overlay re-renders when these flip, not when
  // the message text grows.
  const isStreaming = useChatStore((s) => s.isStreaming);
  // Both selectors return a PRIMITIVE, so this component re-renders about twice
  // a turn (when the kind flips, when the turn changes) rather than once per
  // streamed delta — the panel below owns the delta-sensitive subscription.
  const outputKind = useChatStore((s) => turnOutputKind(s.messages));
  const turnKey = useChatStore((s) => currentTurnKey(s.messages));

  // The ✕ hides the TEXT, keyed on the turn it was pressed in. A new turn has a
  // new key, so the next reply shows by itself — the hide can never leak
  // forward, and nothing has to remember to clear it.
  //
  // Seeded with the turn that was already on screen when voice mode opened, so
  // entering does not immediately dump the last typed reply over the sphere. It
  // hides TEXT only: an approval card open at that moment still renders, which
  // matters now that answering it in the chat is no longer the only way.
  const [hiddenTurn, setHiddenTurn] = useState<string | null>(() =>
    currentTurnKey(useChatStore.getState().messages)
  );
  const textHidden = hiddenTurn !== null && hiddenTurn === turnKey;

  const hasCard = outputKind === 'card' || outputKind === 'both';
  const awaitingDecision = hasCard;
  // The panel is what shrinks the sphere. A lone "show reply" chip does not.
  const panelOpen = hasCard || (outputKind === 'text' && !textHidden);

  const speakingSettled = useSettledFlag(speaking, SPEAKING_SETTLE_MS);

  // Order matters: a live mic or an in-flight transcription always outranks a
  // debounced speaking tail, so the settle window can never mask a mic that
  // has genuinely re-opened.
  const state: OrbState = useMemo(() => {
    if (voiceError) return 'error';
    if (phase === 'recording') return 'listening';
    if (phase === 'transcribing') return 'thinking';
    if (speakingSettled) return 'speaking';
    if (isStreaming) return 'thinking';
    return 'idle';
  }, [voiceError, phase, speakingSettled, isStreaming]);

  const caption =
    voiceError ??
    (state === 'listening'
      ? interimText || 'Listening…'
      : state === 'thinking'
        ? phase === 'transcribing'
          ? 'One moment…'
          : 'Thinking…'
        : state === 'speaking'
          ? 'Speaking…'
          : awaitingDecision
            ? 'Waiting on your decision'
            // The mic stays open for as long as voice mode is (voiceStore's
            // OPEN-MIC rule), so 'idle' here means something genuinely stopped
            // it — an error, or a decision. "Tap to talk" is the honest label
            // for that, and it is now the exception rather than the norm.
            : 'Tap the sphere to talk');

  // Esc exits. Owned here rather than in the composer because the overlay is
  // the thing that is always mounted while voice mode is open — and ChatInput
  // (which binds Esc to cancelHold) is unmounted for the duration, so the two
  // handlers are never live at once.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        exitVoiceMode();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  // ⚠️ NO exitVoiceMode() ON UNMOUNT, deliberately. React StrictMode
  // double-invokes effects in dev, so an unmount cleanup that cleared the flag
  // would fire the moment voice mode opened and close it again. The
  // "a stale flag can never override the user's settings" guarantee lives in
  // voiceModeActive() instead, which is derived and cannot be defeated by a
  // teardown that does or does not run. The mic is closed by VoiceComposer's
  // unmount, mirroring ChatInput.

  const tappable = state !== 'thinking';

  return (
    <div
      className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-4 px-8 py-5 animate-fade-in"
      role="region"
      aria-label="Voice conversation"
      style={{
        background: 'rgba(4, 7, 16, 0.96)',
        backdropFilter: 'blur(10px)',
        WebkitBackdropFilter: 'blur(10px)',
        backgroundImage: [
          'linear-gradient(rgba(34, 211, 238, 0.042) 1px, transparent 1px)',
          'linear-gradient(90deg, rgba(34, 211, 238, 0.042) 1px, transparent 1px)',
        ].join(', '),
        backgroundSize: '42px 42px',
      }}
    >
      {/* Sphere + caption. `flex-1` WITHOUT `min-h-0`, deliberately: its
          min-content height (the sphere's 96px floor + the caption) is what
          stops it being squeezed to nothing and spilling its children out of
          the region — MEASURED at 900x540, where `min-h-0` gave this block 0px
          and the sphere overflowed upward. The panel below is the shrinkable
          one. */}
      <div className="flex-1 w-full flex flex-col items-center justify-center gap-4">
        {/* The canvas neural sphere handles its own hover-dispersal effect
            internally via mouseenter/leave on this container div. No external
            hover styling needed — the dispersal animation IS the feedback.

            ⚠️ NO CSS TRANSITION ON THE SIZE. VoiceOrb re-runs setup() from a
            ResizeObserver — reallocating the canvas and its 95 stars — so an
            animated width/height would do that on every frame of the
            transition, on a machine whose whole perf story is "it lags". The
            panel's own slide-up is what reads as the cause of the resize.

            ⚠️ THE SPHERE TAKES THE LEFTOVER HEIGHT, IT DOES NOT CLAIM ONE.
            MEASURED: a fixed square (even with max-height:100%) overflowed the
            message region at every window below 1440x900, because the caption
            and the gaps also need room out of the same box. So this wrapper is
            `flex-1` and the caption is `shrink-0` — the caption takes its space
            first, the sphere is `height:100%` of whatever is left, capped by
            the max and kept square by aspect-ratio. The 96px floor is what
            keeps it a usable tap target on a short window; the panel below is
            the thing that gives way instead. */}
        <div
          className="flex-1 w-full flex items-center justify-center"
          style={{ minHeight: '96px' }}
        >
          <div
            className="relative"
            style={{
              height: '100%',
              maxHeight: panelOpen ? 'min(30vh, 240px)' : 'min(52vh, 400px)',
              aspectRatio: '1 / 1',
              maxWidth: '100%',
              overflow: 'visible',
            }}
          >
            <button
              type="button"
              onClick={toggleVoiceListening}
              disabled={!tappable}
              aria-label={
                state === 'listening'
                  ? 'Send what you have said'
                  : state === 'speaking'
                    ? 'Interrupt Furi and talk'
                    : 'Start talking'
              }
              className={clsx(
                'relative w-full h-full rounded-full outline-none',
                'focus-visible:ring-2 focus-visible:ring-cyan-400/60',
                tappable ? 'cursor-pointer' : 'cursor-default'
              )}
              style={{ overflow: 'visible' }}
            >
              <VoiceOrb state={state} className="w-full h-full" />
            </button>
          </div>
        </div>

        {/* Caption. aria-live so the state is announced, not just coloured. */}
        <div className="shrink-0 flex flex-col items-center gap-2 text-center max-w-xl">
          <p
            aria-live="polite"
            className={clsx(
              'selectable text-sm leading-relaxed min-h-[2.5rem] flex items-center',
              voiceError
                ? 'text-danger font-mono text-xs'
                : state === 'listening' && interimText
                  ? 'text-slate-200'
                  : 'text-slate-400 font-mono text-xs tracking-wide uppercase'
            )}
          >
            {caption}
          </p>

          <p className="text-[10px] text-muted/50 font-mono">
            Esc or ✕ to leave voice mode
          </p>
        </div>
      </div>

      {/* The reading surface. The approval card renders HERE now — the old
          "view in chat" button was the only way to answer one, and it worked by
          throwing the user out of voice mode. */}
      {outputKind !== 'none' && (
        <VoiceOutputPanel
          textHidden={textHidden}
          onToggleText={() => setHiddenTurn(textHidden ? null : turnKey)}
        />
      )}
    </div>
  );
}
