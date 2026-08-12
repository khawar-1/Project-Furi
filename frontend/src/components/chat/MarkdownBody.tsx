/**
 * Furi — Assistant markdown body
 *
 * Extracted from MessageBubble so the voice-mode output panel renders a reply
 * EXACTLY as the chat does. Two copies of "how an answer looks" would drift the
 * moment one gained a renderer the other lacked, and the voice panel is the one
 * surface where a mis-rendered list is the whole point of the feature.
 */
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

interface MarkdownBodyProps {
  /** Already-resolved text. The caller owns any placeholder for an empty
   *  streaming message — the two surfaces want different ones. */
  children: string;
}

export function MarkdownBody({ children }: MarkdownBodyProps) {
  return (
    <div className="prose-jarvis">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // Override code blocks for syntax highlighting
          code({ className, children, ...props }) {
            const isInline = !className;
            if (isInline) {
              return (
                <code
                  className="font-mono text-cyan-300 bg-surface-1 px-1.5 py-0.5 rounded text-xs"
                  {...props}
                >
                  {children}
                </code>
              );
            }
            return (
              <pre className="bg-surface-1 border border-surface-border rounded-lg p-4 overflow-x-auto my-3">
                <code
                  className="font-mono text-slate-300 text-xs leading-relaxed"
                  {...props}
                >
                  {children}
                </code>
              </pre>
            );
          },
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
