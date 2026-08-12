/**
 * Furi OS — Chat Panel
 * Main conversation interface with message list, streaming, and input bar.
 *
 * Phase 7, Part 4 — spoken responses: the header hosts the speaker toggle
 * (writes through to the persisted output_enabled — one source of truth with
 * the Part 5 VoiceCard), a "Speaking" indicator, and a stop button while
 * audio plays. Toggling off (and stopping) silences playback instantly.
 */
import { useEffect, useRef, useCallback, useState } from 'react';
import { clsx } from 'clsx';
import { Trash2, Zap, Volume2, VolumeX, Square, AudioLines } from 'lucide-react';
import { useChatStore } from '@/stores/chatStore';
import { useVoiceStore } from '@/stores/voiceStore';
import { useUIStore } from '@/stores/uiStore';
import { useShallow } from 'zustand/react/shallow';
import { voiceApi, voiceUpdatePayload } from '@/lib/api';
import { stopSpeaking } from '@/lib/voiceOutput';
import { VoiceModeOverlay } from '@/components/voice/VoiceModeOverlay';
import { VoiceComposer } from '@/components/voice/VoiceComposer';
import { MessageBubble } from './MessageBubble';
import { ChatInput } from './ChatInput';
import { StreamingIndicator } from './StreamingIndicator';

/** Greeting by time of day. Deliberately does NOT address the user by a
 *  placeholder: the old copy read "Good to see you, Human", which is the single
 *  most AI-ish thing in the product — the app has no idea what the user is
 *  called at this point, and a stand-in noun is worse than no name at all. */
function greeting(now = new Date()): string {
  const h = now.getHours();
  if (h < 5) return 'Still up';
  if (h < 12) return 'Good morning';
  if (h < 18) return 'Good afternoon';
  return 'Good evening';
}

/**
 * ⚠️ THE STARTER PROMPTS ASK FURI TO DO THINGS, NOT TO DESCRIBE ITSELF.
 * All four used to be about the product ("What can you do?", "Tell me about
 * your memory system", "How do tools work?", "What's planned for future
 * phases?") — the last one naming our internal build phases. A first screen
 * whose every suggestion is a question about the assistant reads like a demo;
 * these are things someone actually opens Furi to get done, and each one
 * exercises a different capability that really exists.
 */
const STARTERS = [
  "What's on my calendar today?",
  'Any new email I should know about?',
  'Find the PDF about cloud computing',
  'Remind me at 6 to call Jamil',
];

function EmptyState() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-7 px-8 animate-fade-in">
      {/* Mark */}
      <div className="relative">
        <div className="flex h-16 w-16 items-center justify-center rounded-2xl border border-cyan-500/25 bg-gradient-to-br from-cyan-500/10 to-blue-600/10">
          <Zap size={28} className="text-cyan-400" />
        </div>
      </div>

      {/* Greeting */}
      <div className="max-w-md space-y-2 text-center">
        <h1 className="text-2xl font-semibold text-slate-100">{greeting()}</h1>
        <p className="text-sm leading-relaxed text-slate-500">
          Ask me anything, or give me something to do — I can search your files, handle
          your mail and calendar, drive a browser, and act on this machine.
        </p>
      </div>

      {/* Starter prompts */}
      <div className="flex max-w-lg flex-wrap justify-center gap-2">
        {STARTERS.map((suggestion) => (
          <button
            key={suggestion}
            className="rounded-full border border-surface-border bg-surface-2/60 px-3.5 py-1.5 text-xs text-slate-400 outline-none transition-colors hover:border-cyan-500/40 hover:bg-surface-2 hover:text-cyan-300 focus-visible:ring-2 focus-visible:ring-cyan-400/50"
            onClick={() => {
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
  const { messages, isStreaming, sendMessage, clearConversation, error } = useChatStore(
    useShallow((state) => ({
      messages: state.messages,
      isStreaming: state.isStreaming,
      sendMessage: state.sendMessage,
      clearConversation: state.clearConversation,
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
  const isVoiceMode = useUIStore((state) => state.isVoiceMode);
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

  // `inert` is a real Chromium attribute (this app only ever runs in Electron)
  // but React 18's typings predate it, hence the cast.
  const coveredProps = isVoiceMode
    ? ({ inert: '' } as unknown as React.HTMLAttributes<HTMLDivElement>)
    : {};

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
          <h2 className="text-sm font-semibold text-slate-200">Furi Chat</h2>
          {isVoiceMode && (
            <span className="flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-cyan-500/10 border border-cyan-500/30 text-[10px] font-mono text-cyan-400">
              <AudioLines size={10} />
              Voice mode
            </span>
          )}
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
                  ? 'Spoken replies are on — click to mute Furi'
                  : 'Spoken replies are off — click to unmute Furi'
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
          {/* ⚠️ THE PROVIDER SELECTOR WAS REMOVED, AND IT WAS A DEAD CONTROL
              THAT ALSO REPORTED THE WRONG ANSWER. `ChatRequest.provider` is
              accepted by the backend and IGNORED — `get_llm_provider()` returns
              `create_provider()`, which reads `settings.LLM_PROVIDER` — so
              choosing "Groq" changed nothing. Worse, the list did not contain
              deepseek, the provider actually configured, so the header sat
              there displaying "Gemini" while every reply came from DeepSeek.
              CLAUDE.md states the intended mechanism: "Switching providers is a
              .env + backend restart, no code changes." The live provider and
              model are shown truthfully in the StatusBar, read from /health. */}

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

      {/* Messages. `relative` anchors the voice-mode overlay to THIS region —
          the header, sidebar and status bar stay live and usable.
          ⚠️ The list stays MOUNTED underneath while voice mode covers it, so
          scroll position survives and leaving voice mode is instant. */}
      {/* ⚠️ THE OVERLAY IS A SIBLING OF THE SCROLLER, NOT A CHILD OF IT.
          An absolutely positioned child of a scroll container is anchored to the
          top of the scrollable CONTENT, not to the visible viewport — so with a
          long conversation (which this panel auto-scrolls to the bottom of on
          every message) the sphere sat above the visible area entirely. This
          outer box is the positioned, non-scrolling layer; the inner one
          scrolls. It is also what lets the voice panel bound its height against
          the region the user can actually see. */}
      <div className="flex-1 min-h-0 relative">
        <div className="absolute inset-0 overflow-y-auto">
          {/* `display: contents` adds no box, so EmptyState's h-full still
              resolves against the scroll container above. `inert` takes the
              covered chat out of the tab order — without it, focus could still
              reach a PlanCard's Approve button sitting behind the sphere. */}
          <div
            style={{ display: 'contents' }}
            aria-hidden={isVoiceMode || undefined}
            {...coveredProps}
          >
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
        </div>

        {isVoiceMode && <VoiceModeOverlay />}
      </div>

      {/* Input. A SWAP, not a hide: ChatInput owns window-level Ctrl+Space and
          Escape bindings that would fight the overlay's — see VoiceComposer. */}
      <div className="flex-shrink-0 border-t border-surface-border">
        {isVoiceMode ? (
          <VoiceComposer />
        ) : (
          <ChatInput
            onSend={sendMessage}
            isStreaming={isStreaming}
            disabled={false}
          />
        )}
      </div>
    </div>
  );
}
