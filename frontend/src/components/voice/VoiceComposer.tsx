/**
 * Jarvis OS — Voice Mode composer
 *
 * Replaces ChatInput while voice mode is open: a large mic that is the main
 * control, a field you can still type into, and an ✕ that leaves.
 *
 * ⚠️ A SWAP, NOT A HIDE. ChatInput binds Ctrl+Space and Escape at WINDOW level
 * and cancels any live recording on unmount. Leaving it mounted and merely
 * hidden would put two Escape handlers on the same key with opposite meanings.
 * Because the two composers are never mounted together, the bindings can never
 * collide — and ChatInput's unmount cancel is exactly the reset wanted on the
 * way in, since voice mode opens its own fresh capture immediately after.
 *
 * Typing here is deliberate: the reply is still spoken (voice mode implies
 * output), so you can switch to the keyboard for a name or a URL that is hard
 * to say without leaving the conversation.
 */
import { useCallback, useEffect, useRef } from 'react';
import { clsx } from 'clsx';
import { Mic, Send, Square, X } from 'lucide-react';
import { useShallow } from 'zustand/react/shallow';
import { useChatStore } from '@/stores/chatStore';
import { useVoiceStore } from '@/stores/voiceStore';
import {
  exitVoiceMode,
  sendFromVoiceMode,
  toggleVoiceListening,
} from '@/lib/voiceModeControl';

export function VoiceComposer() {
  const { draftMessage, setDraftMessage, isStreaming } = useChatStore(
    useShallow((s) => ({
      draftMessage: s.draftMessage,
      setDraftMessage: s.setDraftMessage,
      isStreaming: s.isStreaming,
    }))
  );
  const { phase, speaking, voiceError, clearVoiceError, cancelHold } = useVoiceStore(
    useShallow((s) => ({
      phase: s.phase,
      speaking: s.speaking,
      voiceError: s.error,
      clearVoiceError: s.clearError,
      cancelHold: s.cancelHold,
    }))
  );
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const isRecording = phase === 'recording';
  const isTranscribing = phase === 'transcribing';
  const canSend = draftMessage.trim().length > 0 && !isStreaming;

  const handleSend = useCallback(() => {
    const trimmed = draftMessage.trim();
    if (!trimmed || isStreaming) return;
    sendFromVoiceMode(trimmed);
    setDraftMessage('');
    if (inputRef.current) inputRef.current.style.height = 'auto';
  }, [draftMessage, isStreaming, setDraftMessage]);

  // If this composer unmounts mid-recording (leaving voice mode, a panel
  // switch), never leave the mic open. The same cleanup ChatInput has carried
  // since Phase 7 — and the reason the overlay does NOT clear the voice-mode
  // flag here: cancelHold is harmless when idle, clearing the flag is not
  // (StrictMode runs this cleanup once on mount in dev).
  useEffect(() => () => cancelHold(), [cancelHold]);

  // A transcript can land here without a keystroke (review mode is overridden
  // in voice mode, but a turn that arrives mid-stream still drafts) — keep the
  // field sized to whatever appears in it.
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
  }, [draftMessage]);

  const micLabel = isRecording
    ? 'Send what you have said'
    : isTranscribing
      ? 'Transcribing…'
      : speaking
        ? 'Interrupt Jarvis and talk'
        : 'Start talking';

  return (
    <div className="px-4 pb-4 pt-2">
      {voiceError && (
        <div className="flex items-center gap-2 mb-2 px-3 py-1.5 rounded-lg bg-danger/10 border border-danger/30 text-danger text-xs font-mono">
          <span className="flex-1 min-w-0 truncate">⚠ {voiceError}</span>
          <button
            type="button"
            onClick={clearVoiceError}
            className="flex-shrink-0 hover:text-red-300"
            title="Dismiss"
          >
            <X size={12} />
          </button>
        </div>
      )}

      <div
        className={clsx(
          'flex items-center gap-3 rounded-2xl border p-2.5 transition-all duration-200 bg-surface-2',
          isRecording
            ? 'border-cyan-400/60 shadow-glow-cyan'
            : 'border-cyan-500/25 shadow-glow-sm'
        )}
      >
        <textarea
          ref={inputRef}
          id="voice-mode-input"
          value={draftMessage}
          onChange={(e) => setDraftMessage(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              handleSend();
            }
          }}
          placeholder="Message Jarvis… (type or speak)"
          rows={1}
          className={clsx(
            'flex-1 bg-transparent text-sm text-slate-200 placeholder-muted resize-none outline-none',
            'leading-relaxed min-h-[24px] max-h-[120px] overflow-y-auto selectable font-sans px-1.5'
          )}
          style={{ height: 'auto' }}
        />

        {canSend && (
          <button
            id="voice-mode-send-btn"
            type="button"
            onClick={handleSend}
            title="Send"
            className="w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0 bg-surface-3 text-cyan-400 hover:bg-cyan-500 hover:text-white transition-all duration-150"
          >
            <Send size={14} />
          </button>
        )}

        {/* The main control. Deliberately oversized — in voice mode this is the
            thing the eye should land on, not the text field. */}
        <button
          id="voice-mode-mic-btn"
          type="button"
          onClick={toggleVoiceListening}
          disabled={isTranscribing}
          title={micLabel}
          aria-label={micLabel}
          className={clsx(
            'relative w-12 h-12 rounded-full flex items-center justify-center flex-shrink-0',
            'transition-all duration-200 select-none',
            isRecording
              ? 'bg-cyan-400 text-surface shadow-glow-cyan scale-105'
              : isTranscribing
                ? 'bg-surface-3 text-cyan-400 cursor-wait'
                : 'bg-gradient-to-br from-cyan-500 to-blue-600 text-white shadow-glow-cyan hover:from-cyan-400 hover:to-blue-500'
          )}
        >
          {isRecording && (
            <span className="absolute inset-0 rounded-full bg-cyan-400/40 animate-ping" />
          )}
          {speaking && !isRecording ? <Square size={16} fill="currentColor" /> : <Mic size={18} />}
        </button>

        <button
          id="voice-mode-exit-btn"
          type="button"
          onClick={exitVoiceMode}
          title="End voice mode (Esc)"
          aria-label="End voice mode"
          className="w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0 text-muted hover:text-danger hover:bg-danger/10 transition-fast"
        >
          <X size={16} />
        </button>
      </div>

      <p className="text-center text-[10px] text-muted/40 mt-2 font-mono">
        speak freely · tap the sphere to interrupt · esc to leave
      </p>
    </div>
  );
}
