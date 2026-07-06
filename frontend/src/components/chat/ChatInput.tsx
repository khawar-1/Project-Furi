/**
 * Jarvis OS — Chat Input
 * Multi-line textarea with keyboard shortcuts and send button.
 * Enter to send, Shift+Enter for newline.
 */
import { useState, useRef, useCallback, useEffect } from 'react';
import { clsx } from 'clsx';
import { Send, Mic } from 'lucide-react';
import { useChatStore } from '@/stores/chatStore';
import { useShallow } from 'zustand/react/shallow';

interface ChatInputProps {
  onSend: (content: string) => void;
  isStreaming: boolean;
  disabled?: boolean;
}

export function ChatInput({ onSend, isStreaming, disabled = false }: ChatInputProps) {
  const { draftMessage, setDraftMessage } = useChatStore(
    useShallow((state) => ({
      draftMessage: state.draftMessage,
      setDraftMessage: state.setDraftMessage,
    }))
  );
  const textareaRef = useRef<HTMLTextAreaElement>(null);

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

  const canSend = draftMessage.trim().length > 0 && !isStreaming && !disabled;

  return (
    <div className="px-4 pb-4 pt-2">
      <div
        className={clsx(
          'flex items-end gap-3 rounded-2xl border p-3 transition-all duration-200',
          'bg-surface-2',
          canSend || draftMessage.length > 0
            ? 'border-cyan-500/40 shadow-glow-sm'
            : 'border-surface-border'
        )}
      >
        {/* Textarea */}
        <textarea
          ref={textareaRef}
          id="chat-input"
          value={draftMessage}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          placeholder="Message Jarvis... (Enter to send, Shift+Enter for newline)"
          disabled={disabled}
          rows={1}
          className={clsx(
            'flex-1 bg-transparent text-sm text-slate-200 placeholder-muted resize-none outline-none',
            'leading-relaxed min-h-[24px] max-h-[200px] overflow-y-auto selectable',
            'font-sans'
          )}
          style={{ height: 'auto' }}
        />

        {/* Action Buttons */}
        <div className="flex items-center gap-2 flex-shrink-0">
          {/* Voice Button (placeholder for Phase 6) */}
          <button
            id="voice-input-btn"
            type="button"
            title="Voice input (Phase 6)"
            disabled
            className="w-8 h-8 rounded-lg flex items-center justify-center text-muted opacity-40 cursor-not-allowed"
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
      </p>
    </div>
  );
}
