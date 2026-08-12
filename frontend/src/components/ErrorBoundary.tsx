/**
 * Furi OS — App-wide render safety net.
 *
 * Without this, an uncaught error in ANY component's render unmounts the entire
 * React tree and leaves a blank window (in Electron: just the native menu bar,
 * nothing else). This catches such errors, keeps the app alive, and offers a
 * reload — the UI is never a black rectangle again.
 *
 * Note: this catches React RENDER errors only. A native renderer-process crash
 * (e.g. Web Audio decodeAudioData on a huge/odd file) cannot be caught here —
 * those must be prevented at the source (guard inputs before decoding).
 */
import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surface it in the console (DevTools) for diagnosis; never rethrow.
    console.error('Uncaught render error:', error, info.componentStack);
  }

  private handleReload = (): void => {
    window.location.reload();
  };

  private handleDismiss = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100vh',
          width: '100vw',
          gap: 16,
          padding: 32,
          textAlign: 'center',
          background: '#0A0A0F',
          color: '#e2e8f0',
          fontFamily: 'system-ui, sans-serif',
        }}
      >
        <div style={{ fontSize: 18, fontWeight: 600 }}>Something went wrong</div>
        <div style={{ fontSize: 13, color: '#94a3b8', maxWidth: 480 }}>
          A part of the interface hit an error and stopped rendering. Your data is safe — this
          only affects the current view.
        </div>
        <pre
          style={{
            fontSize: 11,
            color: '#f87171',
            maxWidth: 560,
            maxHeight: 160,
            overflow: 'auto',
            whiteSpace: 'pre-wrap',
            background: 'rgba(248,113,113,0.08)',
            border: '1px solid rgba(248,113,113,0.2)',
            borderRadius: 8,
            padding: 12,
            margin: 0,
          }}
        >
          {error.message}
        </pre>
        <div style={{ display: 'flex', gap: 10 }}>
          <button
            onClick={this.handleReload}
            style={{
              fontSize: 13,
              padding: '8px 16px',
              borderRadius: 8,
              cursor: 'pointer',
              color: '#22d3ee',
              background: 'rgba(34,211,238,0.1)',
              border: '1px solid rgba(34,211,238,0.25)',
            }}
          >
            Reload app
          </button>
          <button
            onClick={this.handleDismiss}
            style={{
              fontSize: 13,
              padding: '8px 16px',
              borderRadius: 8,
              cursor: 'pointer',
              color: '#94a3b8',
              background: 'rgba(148,163,184,0.08)',
              border: '1px solid rgba(148,163,184,0.2)',
            }}
          >
            Try to continue
          </button>
        </div>
      </div>
    );
  }
}
