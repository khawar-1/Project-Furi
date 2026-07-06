/**
 * Jarvis OS — GlowBorder
 * Reusable wrapper that applies HUD-style glow border effects.
 */
import { clsx } from 'clsx';
import type { ReactNode } from 'react';

interface GlowBorderProps {
  children: ReactNode;
  active?: boolean;
  className?: string;
  color?: 'cyan' | 'blue';
}

export function GlowBorder({ children, active = false, className, color = 'cyan' }: GlowBorderProps) {
  return (
    <div
      className={clsx(
        'relative rounded-lg border transition-all duration-200',
        active
          ? color === 'cyan'
            ? 'border-cyan-500/50 shadow-glow-cyan'
            : 'border-blue-500/50 shadow-glow-blue'
          : 'border-surface-border',
        className
      )}
    >
      {children}
    </div>
  );
}
