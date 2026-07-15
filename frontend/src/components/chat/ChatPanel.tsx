/**
 * Jarvis OS — Chat Panel
 * Main conversation interface with message list, streaming, and input bar.
 *
 * Phase 7, Part 4 — spoken responses: the header hosts the speaker toggle
 * (writes through to the persisted output_enabled — one source of truth with
 * the Part 5 VoiceCard), a "Speaking" indicator, and a stop button while
 * audio plays. Toggling off (and stopping) silences playback instantly.
 */
import { useEffect, useRef, useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { Trash2, Zap, ChevronDown, Volume2, VolumeX, Square } from 'lucide-react';
import { useChatStore } from '@/stores/chatStore';
import { useVoiceStore } from '@/stores/voiceStore';
import { useShallow } from 'zustand/react/shallow';
import { voiceApi, voiceUpdatePayload } from '@/lib/api';
import { stopSpeaking } from '@/lib/voiceOutput';
import { MessageBubble } from './MessageBubble';
import { ChatInput } from './ChatInput';
import { StreamingIndicator } from './StreamingIndicator';
import type { LLMProviderName } from '@/types';

const PROVIDERS: Array<{ value: LLMProviderName; label: string }> = [
  { value: 'gemini', label: 'Gemini' },
  { value: 'groq', label: 'Groq' },
  { value: 'ollama', label: 'Ollama' },
  { value: 'openrouter', label: 'OpenRouter' },
];

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-6 px-8 animate-fade-in">
      {/* Logo */}
      <div className="relative">
        <div className="w-20 h-20 rounded-2xl bg-gradient-to-br from-cyan-500/10 to-blue-600/10 hud-border-active flex items-center justify-center animate-glow-pulse">
          <Zap size={32} className="text-cyan-400 text-glow-cyan" />
        </div>
        <div className="absolute -top-1 -right-1 w-4 h-4 rounded-full bg-emerald-500 border-2 border-surface animate-pulse" />
      </div>

      {/* Greeting */}
      <div className="text-center space-y-2 max-w-md">
        <h1 className="text-2xl font-semibold text-slate-100">
          Good to see you, <span className="text-cyan-400 text-glow-cyan">Human</span>
        </h1>
        <p className="text-slate-500 text-sm leading-relaxed">
          I'm Jarvis, your personal AI operating system. I remember your preferences,
          know your contacts, and can execute tasks across your digital life.
        </p>
      </div>

      {/* Suggestion chips */}
      <div className="flex flex-wrap gap-2 justify-center max-w-lg">
        {[
          'What can you do?',
          'Tell me about your memory system',
          'How do tools work?',
          'What\'s planned for future phases?',
        ].map((suggestion) => (
          <button
            key={suggestion}
            className="px-3 py-1.5 rounded-full bg-surface-2 border border-surface-border text-xs text-slate-400 hover:border-cyan-500/40 hover:text-cyan-400 transition-smooth"
            onClick={() => {
              // Will be wired below via ref
              const event = new CustomEvent('jarvis:suggestion', { detail: suggestion });
              window.dispatchEvent(event);
            }}
          >
            {suggestion}
          </button>
        ))}
      </div>
    </div>
  );
}

export function ChatPanel() {
  const { messages, isStreaming, sendMessage, clearConversation, activeProvider, setProvider, error } =
    useChatStore(
      useShallow((state) => ({
        messages: state.messages,
        isStreaming: state.isStreaming,
        sendMessage: state.sendMessage,
        clearConversation: state.clearConversation,
        activeProvider: state.activeProvider,
        setProvider: state.setProvider,
        error: state.error,
      }))
    );
  const { voiceSettings, speaking, applySettings } = useVoiceStore(
    useShallow((state) => ({
      voiceSettings: state.settings,
      speaking: state.speaking,
      applySettings: state.applySettings,
    }))
  );
  // A speaker-toggle PUT is in flight — ignore further clicks until settled.
  const [togglingSpeaker, setTogglingSpeaker] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const hasMessages = messages.length > 0;

  // Speaker toggle: writes through to the persisted output_enabled (one
  // source of truth with Settings). Optimistic + revert on error — the
  // FileIndexCard enable-flow pattern. Turning OFF silences instantly.
  const toggleSpeaker = useCallback(async () => {
    if (!voiceSettings || togglingSpeaker) return;
    const next = !voiceSettings.output_enabled;
    if (!next) stopSpeaking();
    setTogglingSpeaker(true);
    applySettings({ ...voiceSettings, output_enabled: next });
    try {
      const updated = await voiceApi.updateSettings(
        voiceUpdatePayload(voiceSettings, { output_enabled: next })
      );
      applySettings(updated);
    } catch {
      applySettings(voiceSettings); // revert — the backend never saw it
    } finally {
      setTogglingSpeaker(false);
    }
  }, [voiceSettings, togglingSpeaker, applySettings]);

  // Auto-scroll to bottom on new messages
  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  // Handle suggestion chip clicks
  useEffect(() => {
    const handler = (e: CustomEvent<string>) => {
      sendMessage(e.detail);
    };
    window.addEventListener('jarvis:suggestion', handler as EventListener);
    return () => window.removeEventListener('jarvis:suggestion', handler as EventListener);
  }, [sendMessage]);

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between h-14 px-4 border-b border-surface-border bg-surface-1 flex-shrink-0">
        <div className="flex items-center gap-3">
          <div className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse shadow-sm" style={{ boxShadow: '0 0 6px rgba(16, 185, 129, 0.6)' }} />
          <h2 className="text-sm font-semibold text-slate-200">Jarvis Chat</h2>
          {isStreaming && (
            <span className="text-[10px] font-mono text-cyan-400/70 animate-pulse-cyan">
              ● Generating
            </span>
          )}
          {speaking && (
            <span className="flex items-center gap-1.5 text-[10px] font-mono text-emerald-400/80 animate-pulse">
              ● Speaking
              <button
                id="voice-stop-btn"
                onClick={() => stopSpeaking()}
                title="Stop speaking"
                className="w-5 h-5 rounded flex items-center justify-center text-emerald-400 hover:text-danger hover:bg-danger/10 transition-fast"
              >
                <Square size={10} fill="currentColor" />
              </button>
            </span>
          )}
        </div>

        <div className="flex items-center gap-3">
          {/* Speaker toggle (Phase 7, Part 4) — persisted output_enabled */}
          {voiceSettings?.enabled && (
            <button
              id="voice-output-toggle"
              onClick={() => void toggleSpeaker()}
              disabled={togglingSpeaker}
              title={
                voiceSettings.output_enabled
                  ? 'Spoken replies are on — click to mute Jarvis'
                  : 'Spoken replies are off — click to unmute Jarvis'
              }
              className={clsx(
                'w-7 h-7 rounded-lg flex items-center justify-center transition-fast',
                voiceSettings.output_enabled
                  ? 'text-cyan-400 hover:bg-surface-3'
                  : 'text-muted hover:text-cyan-400 hover:bg-surface-3'
              )}
            >
              {voiceSettings.output_enabled ? <Volume2 size={14} /> : <VolumeX size={14} />}
            </button>
          )}
          {/* Provider selector */}
          <div className="relative">
            <select
              id="provider-selector"
              value={activeProvider}
              onChange={(e) => setProvider(e.target.value as LLMProviderName)}
              className="appearance-none pl-2 pr-7 py-1 rounded-lg bg-surface-2 border border-surface-border text-xs text-slate-400 font-mono cursor-pointer hover:border-cyan-500/40 transition-fast outline-none focus:border-cyan-500/60"
            >
              {PROVIDERS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </select>
            <ChevronDown size={10} className="absolute right-2 top-1/2 -translate-y-1/2 text-muted pointer-events-none" />
          </div>

          {/* Clear conversation */}
          {hasMessages && (
            <button
              id="clear-chat-btn"
              onClick={clearConversation}
              title="Clear conversation"
              className="w-7 h-7 rounded-lg flex items-center justify-center text-muted hover:text-danger hover:bg-danger/10 transition-fast"
            >
              <Trash2 size={14} />
            </button>
          )}
        </div>
      </div>

      {/* Error Banner */}
      {error && (
        <div className="mx-4 mt-3 px-4 py-2 rounded-lg bg-danger/10 border border-danger/30 text-danger text-xs font-mono">
          ⚠ {error}
        </div>
      )}

      {/* Messages */}
      <div className="flex-1 overflow-y-auto">
        {!hasMessages ? (
          <EmptyState />
        ) : (
          <div className="py-4">
            {messages.map((message) => (
              <MessageBubble key={message.id} message={message} />
            ))}

            {/* Show streaming indicator only when the assistant hasn't started outputting yet */}
            {isStreaming &&
              messages[messages.length - 1]?.role === 'assistant' &&
              messages[messages.length - 1]?.content === '' && (
                <StreamingIndicator />
              )}

            <div ref={messagesEndRef} />
          </div>
        )}
      </div>

      {/* Input */}
      <div className="flex-shrink-0 border-t border-surface-border">
        <ChatInput
          onSend={sendMessage}
          isStreaming={isStreaming}
          disabled={false}
        />
      </div>
    </div>
  );
}
