/**
 * Jarvis OS — Message Bubble
 * Renders a single chat message with markdown, role-based styling, and timestamp.
 */
import { clsx } from 'clsx';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Zap, User } from 'lucide-react';
import type { ChatMessage } from '@/types';
import { useChatStore } from '@/stores/chatStore';
import { PlanCard } from './PlanCard';

interface MessageBubbleProps {
  message: ChatMessage;
}

function formatTime(date: Date): string {
  return new Intl.DateTimeFormat('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: true,
  }).format(date);
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === 'user';
  const isAssistant = message.role === 'assistant';
  const respondToPlan = useChatStore((s) => s.respondToPlan);
  const respondToChoice = useChatStore((s) => s.respondToChoice);
  const cancelBackgroundTask = useChatStore((s) => s.cancelBackgroundTask);

  // Phase 3: for plans that paused at the approval gate, the PlanCard IS the
  // message — the streamed deterministic text repeats the same step list, so
  // the text bubble is hidden (and would go stale once the plan resolves).
  const hideTextBubble = Boolean(message.plan && message.planNeededApproval);

  return (
    <div
      className={clsx(
        'flex gap-3 w-full px-4 py-3 animate-fade-in group',
        isUser ? 'justify-end' : 'justify-start'
      )}
    >
      {/* Assistant Avatar */}
      {isAssistant && (
        <div className="flex-shrink-0 w-8 h-8 rounded-lg bg-gradient-to-br from-cyan-500/20 to-blue-600/20 hud-border flex items-center justify-center mt-0.5">
          <Zap size={14} className="text-cyan-400" />
        </div>
      )}

      {/* Message Content */}
      <div
        className={clsx(
          'flex flex-col gap-1 max-w-[75%]',
          isUser ? 'items-end' : 'items-start'
        )}
      >
        {/* Role Label */}
        <div className="flex items-center gap-2 px-1">
          <span className="text-[10px] font-mono text-muted uppercase tracking-wider">
            {isUser ? 'You' : 'Jarvis'}
          </span>
          {message.model && isAssistant && (
            <span className="text-[10px] font-mono text-cyan-500/40">
              {message.model}
            </span>
          )}
          <span className="text-[10px] text-muted/50 opacity-0 group-hover:opacity-100 transition-fast">
            {formatTime(message.createdAt instanceof Date ? message.createdAt : new Date(message.createdAt))}
          </span>
        </div>

        {/* Agent plan card (Phase 3 task turns) */}
        {message.plan && (
          <PlanCard
            plan={message.plan}
            responding={Boolean(message.planResponding)}
            error={message.planError ?? null}
            onRespond={(approved) => void respondToPlan(message.id, approved)}
            onAnswer={(answer) => void respondToChoice(message.id, answer)}
            cancelRequested={Boolean(message.planCancelRequested)}
            onCancelTask={() => void cancelBackgroundTask(message.id)}
          />
        )}

        {/* Bubble */}
        {!hideTextBubble && (
        <div
          className={clsx(
            'rounded-2xl px-4 py-3 text-sm leading-relaxed',
            isUser
              ? 'bg-gradient-to-br from-blue-600/20 to-cyan-600/20 border border-blue-500/30 text-slate-200 rounded-tr-md'
              : 'bg-surface-2 border border-surface-border text-slate-200 rounded-tl-md',
            message.isStreaming && 'typing-cursor'
          )}
        >
          {isUser ? (
            <p className="selectable whitespace-pre-wrap">{message.content}</p>
          ) : (
            <div className="prose-jarvis">
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{
                  // Override code blocks for syntax highlighting
                  code({ className, children, ...props }) {
                    const isInline = !className;
                    if (isInline) {
                      return (
                        <code className="font-mono text-cyan-300 bg-surface-1 px-1.5 py-0.5 rounded text-xs" {...props}>
                          {children}
                        </code>
                      );
                    }
                    return (
                      <pre className="bg-surface-1 border border-surface-border rounded-lg p-4 overflow-x-auto my-3">
                        <code className="font-mono text-slate-300 text-xs leading-relaxed" {...props}>
                          {children}
                        </code>
                      </pre>
                    );
                  },
                }}
              >
                {message.content || (message.isStreaming ? '' : '...')}
              </ReactMarkdown>
            </div>
          )}
        </div>
        )}
      </div>

      {/* User Avatar */}
      {isUser && (
        <div className="flex-shrink-0 w-8 h-8 rounded-lg bg-gradient-to-br from-blue-500/20 to-blue-600/20 border border-blue-500/30 flex items-center justify-center mt-0.5">
          <User size={14} className="text-blue-400" />
        </div>
      )}
    </div>
  );
}
