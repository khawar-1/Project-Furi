/**
 * Furi OS — Chat Input
 * Multi-line textarea with keyboard shortcuts and send button.
 * Enter to send, Shift+Enter for newline.
 *
 * Phase 7, Part 2 — push-to-talk: hold the mic button (or Ctrl+Space) to
 * record, release to transcribe; the transcript auto-sends (or lands here in
 * the input box when review_before_send is on). Esc cancels a hold. Voice is
 * a transport: the sent text enters the exact same chat pipeline as typing.
 */
import { useState, useRef, useCallback, useEffect } from 'react';
import { clsx } from 'clsx';
import { Send, Mic, X, AudioLines } from 'lucide-react';
import { useChatStore } from '@/stores/chatStore';
import { useVoiceStore } from '@/stores/voiceStore';
import { voiceCaptureSupported } from '@/lib/voiceInput';
import { enterVoiceMode } from '@/lib/voiceModeControl';
import { useShallow } from 'zustand/react/shallow';

interface ChatInputProps {
  onSend: (content: string) => void;
  isStreaming: boolean;
  disabled?: boolean;
}

/** Inline waveform while recording: a rolling window of live level samples
 *  rendered as bars. Levels arrive via the store at animation-frame rate;
 *  each render shifts the newest sample into a ref-held history. */
function VoiceWaveform({ level }: { level: number }) {
  const BARS = 28;
  const historyRef = useRef<number[]>(Array(BARS).fill(0.05));
  historyRef.current = [...historyRef.current.slice(1), Math.max(0.05, level)];

  return (
    <div className="flex items-center gap-[2px] h-6 flex-1 min-w-0" aria-hidden>
      {historyRef.current.map((v, i) => (
        <div
          key={i}
          className="w-[3px] rounded-full bg-red-400/80 transition-[height] duration-75"
          style={{ height: `${Math.round(4 + v * 20)}px` }}
        />
      ))}
    </div>
  );
}

export function ChatInput({ onSend, isStreaming, disabled = false }: ChatInputProps) {
  const { draftMessage, setDraftMessage } = useChatStore(
    useShallow((state) => ({
      draftMessage: state.draftMessage,
      setDraftMessage: state.setDraftMessage,
    }))
  );
  const {
    voicePhase,
    voiceMode,
    voiceLevel,
    interimText,
    voiceSettings,
    voiceError,
    beginHold,
    endHold,
    cancelHold,
    clearVoiceError,
  } = useVoiceStore(
    useShallow((state) => ({
      voicePhase: state.phase,
      voiceMode: state.mode,
      voiceLevel: state.level,
      interimText: state.interimText,
      voiceSettings: state.settings,
      voiceError: state.error,
      beginHold: state.beginHold,
      endHold: state.endHold,
      cancelHold: state.cancelHold,
      clearVoiceError: state.clearError,
    }))
  );
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Ctrl+Space can auto-repeat keydown while held — only the first one starts.
  const [holdingKey, setHoldingKey] = useState(false);

  const voiceSupported = voiceCaptureSupported();
  const voiceEnabled = !!voiceSettings?.enabled && voiceSupported;
  const sttStatus = voiceSettings?.stt_status.status ?? 'not_loaded';
  const micTitle = !voiceSupported
    ? 'Microphone capture is not supported here'
    : !voiceSettings?.enabled
      ? 'Voice input is off — enable it in Settings'
      : sttStatus === 'loading'
        ? 'The voice model is still downloading — hold to talk once it’s ready'
        : sttStatus === 'error'
          ? `Voice model failed to load: ${voiceSettings?.stt_status.error ?? 'unknown error'}`
          : 'Hold to talk (or hold Ctrl+Space) — release to send, Esc to cancel';
  // Same capability ladder as the mic: voice mode needs the SAME model, so it
  // is available in exactly the same conditions.
  const voiceModeTitle = !voiceSupported
    ? 'Microphone capture is not supported here'
    : !voiceSettings?.enabled
      ? 'Voice mode needs voice turned on in Settings'
      : sttStatus === 'loading'
        ? 'The voice model is still downloading — voice mode opens once it’s ready'
        : sttStatus === 'error'
          ? `Voice model failed to load: ${voiceSettings?.stt_status.error ?? 'unknown error'}`
          : 'Voice mode — a hands-free conversation';

  const handleSend = useCallback(() => {
    const trimmed = draftMessage.trim();
    if (!trimmed || isStreaming || disabled) return;
    onSend(trimmed);
    setDraftMessage('');
    // Reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  }, [draftMessage, isStreaming, disabled, onSend, setDraftMessage]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setDraftMessage(e.target.value);
    // Auto-resize textarea
    const textarea = e.target;
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`;
  };

  // Restore height and focus on mount
  useEffect(() => {
    if (textareaRef.current) {
      if (draftMessage) {
        textareaRef.current.style.height = 'auto';
        textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 200)}px`;
      }
      // Auto-focus with a tiny delay to prevent race conditions with tab switching animations
      const timer = setTimeout(() => {
        textareaRef.current?.focus();
      }, 50);
      return () => clearTimeout(timer);
    }
  }, []); // Only run once on mount

  // The draft can change without a keystroke (a voice transcript landing in
  // review_before_send mode) — keep the textarea sized to its content.
  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`;
  }, [draftMessage]);

  // Hold Ctrl+Space = hold the mic button; Esc cancels a live recording.
  // Window-level so it works wherever focus sits inside the chat panel.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.code === 'Space' && e.ctrlKey && !e.repeat && voiceEnabled) {
        e.preventDefault();
        setHoldingKey(true);
        void beginHold();
        return;
      }
      if (e.key === 'Escape') {
        setHoldingKey(false);
        cancelHold();
      }
    };
    const onKeyUp = (e: KeyboardEvent) => {
      // Releasing either half of the chord ends the hold.
      if (e.code === 'Space' || e.key === 'Control') {
        if (holdingKey) {
          setHoldingKey(false);
          void endHold();
        }
      }
    };
    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('keyup', onKeyUp);
    };
  }, [voiceEnabled, holdingKey, beginHold, endHold, cancelHold]);

  // If this input unmounts mid-hold (panel switch), never leave the mic open.
  useEffect(() => () => cancelHold(), [cancelHold]);

  const canSend = draftMessage.trim().length > 0 && !isStreaming && !disabled;
  const isRecording = voicePhase === 'recording';
  const isTranscribing = voicePhase === 'transcribing';
  // Part 5: a hotkey-summoned recording is hands-free — a mic TAP sends it
  // (there is no button being held to release).
  const isSummonRecording = isRecording && voiceMode === 'summon';

  return (
    <div className="px-4 pb-4 pt-2">
      {/* Voice error — non-blocking, inline, dismissible */}
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
          'flex items-end gap-3 rounded-2xl border p-3 transition-all duration-200',
          'bg-surface-2',
          isRecording
            ? 'border-red-500/50 shadow-glow-sm'
            : canSend || draftMessage.length > 0
              ? 'border-cyan-500/40 shadow-glow-sm'
              : 'border-surface-border'
        )}
      >
        {/* Textarea — replaced by the listening indicator while voice is active */}
        {isRecording ? (
          <div className="flex-1 flex items-center gap-3 min-h-[24px]">
            {/* Part 6: the interim transcript replaces the static hint the
                moment there is one — the words beat the instructions. */}
            {interimText ? (
              <span className="flex items-center gap-2 min-w-0 text-xs font-mono text-slate-300">
                <span className="w-2 h-2 rounded-full bg-red-500 animate-pulse flex-shrink-0" />
                <span className="truncate">{interimText}</span>
              </span>
            ) : (
              <span className="flex items-center gap-2 flex-shrink-0 text-xs font-mono text-red-400 animate-pulse">
                <span className="w-2 h-2 rounded-full bg-red-500" />
                {isSummonRecording
                  ? 'Listening… tap the mic or pause to send, Esc to cancel'
                  : 'Listening… release to send, Esc to cancel'}
              </span>
            )}
            <VoiceWaveform level={voiceLevel} />
          </div>
        ) : isTranscribing ? (
          <div className="flex-1 flex items-center min-h-[24px]">
            <span className="text-xs font-mono text-cyan-400 animate-pulse">
              Transcribing…
            </span>
          </div>
        ) : (
          <textarea
            ref={textareaRef}
            id="chat-input"
            value={draftMessage}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
            placeholder="Message Furi... (Enter to send, Shift+Enter for newline)"
            disabled={disabled}
            rows={1}
            className={clsx(
              'flex-1 bg-transparent text-sm text-slate-200 placeholder-muted resize-none outline-none',
              'leading-relaxed min-h-[24px] max-h-[200px] overflow-y-auto selectable',
              'font-sans'
            )}
            style={{ height: 'auto' }}
          />
        )}

        {/* Action Buttons */}
        <div className="flex items-center gap-2 flex-shrink-0">
          {/* Voice mode — the hands-free sphere. Sits beside push-to-talk
              rather than replacing it: holding the mic for one sentence and
              opening a conversation are different intentions. */}
          <button
            id="voice-mode-btn"
            type="button"
            title={voiceModeTitle}
            aria-label={voiceModeTitle}
            disabled={!voiceEnabled || isRecording || isTranscribing}
            onClick={enterVoiceMode}
            className={clsx(
              'w-8 h-8 rounded-lg flex items-center justify-center transition-all duration-150',
              voiceEnabled && !isRecording && !isTranscribing
                ? 'text-muted hover:text-cyan-400 hover:bg-surface-3'
                : 'text-muted opacity-40 cursor-not-allowed'
            )}
          >
            <AudioLines size={16} />
          </button>

          {/* Push-to-talk mic (Phase 7, Part 2) — hold to record */}
          <button
            id="voice-input-btn"
            type="button"
            title={micTitle}
            disabled={!voiceEnabled || isTranscribing}
            onPointerDown={(e) => {
              if (!voiceEnabled) return;
              // Part 5: a summon recording has no held button — tapping the
              // mic stops it and sends what was said.
              if (isSummonRecording) {
                void endHold();
                return;
              }
              // Capture the pointer so the release fires here even if the
              // cursor drifts off the button mid-hold.
              e.currentTarget.setPointerCapture(e.pointerId);
              void beginHold();
            }}
            onPointerUp={() => {
              // The tap above already ended the summon recording; a pointerup
              // arriving after it must not double-fire.
              if (!isSummonRecording) void endHold();
            }}
            onPointerCancel={() => {
              if (!isSummonRecording) cancelHold();
            }}
            onContextMenu={(e) => e.preventDefault()}
            className={clsx(
              'w-8 h-8 rounded-lg flex items-center justify-center transition-all duration-150 select-none',
              isRecording
                ? 'bg-red-500 text-white animate-pulse'
                : isTranscribing
                  ? 'bg-surface-3 text-cyan-400 animate-pulse cursor-wait'
                  : voiceEnabled
                    ? 'text-muted hover:text-cyan-400 hover:bg-surface-3'
                    : 'text-muted opacity-40 cursor-not-allowed'
            )}
          >
            <Mic size={16} />
          </button>

          {/* Send Button */}
          <button
            id="chat-send-btn"
            type="button"
            onClick={handleSend}
            disabled={!canSend}
            className={clsx(
              'w-8 h-8 rounded-lg flex items-center justify-center transition-all duration-150',
              canSend
                ? 'bg-cyan-500 text-white hover:bg-cyan-400 shadow-glow-cyan'
                : 'bg-surface-3 text-muted cursor-not-allowed'
            )}
          >
            <Send size={14} />
          </button>
        </div>
      </div>

      {/* Keyboard hint */}
      <p className="text-center text-[10px] text-muted/40 mt-2 font-mono">
        ↵ send &nbsp;·&nbsp; ⇧↵ newline
        {voiceEnabled && <> &nbsp;·&nbsp; hold ⌃␣ to talk</>}
      </p>
    </div>
  );
}
