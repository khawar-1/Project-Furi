/**
 * Jarvis OS — Streaming Indicator
 * Animated "thinking" indicator shown while Jarvis is generating a response.
 */
export function StreamingIndicator() {
  return (
    <div className="flex items-center gap-2 px-4 py-2">
      <div className="flex items-center gap-1">
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            className="w-1.5 h-1.5 rounded-full bg-cyan-500"
            style={{
              animation: `typing 1.4s ease-in-out infinite`,
              animationDelay: `${i * 0.2}s`,
            }}
          />
        ))}
      </div>
      <span className="text-xs text-muted font-mono animate-pulse-cyan">
        Jarvis is thinking...
      </span>
    </div>
  );
}
