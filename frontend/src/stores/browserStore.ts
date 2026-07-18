/**
 * Jarvis OS — Browser media state (Zustand, Phase 14, Part 2)
 *
 * The "▶ Playing" indicator's state: what `browse` left playing, and the stop
 * control. Updated live by an onPush('browser_media') handler (App.tsx) and
 * recovered on startup/reload by a one-shot poll of GET /api/browser/media (the
 * contextStore pattern — the push channel has no queue, so a reload needs a poll
 * to catch a window opened before it connected).
 */
import { create } from 'zustand';
import { browserApi } from '@/lib/api';

interface BrowserMediaState {
  playing: boolean;
  title: string;
  url: string;
  stopping: boolean;

  // Phase 14.6: a kept-open commit result window (the "File Uploaded!" page).
  windowOpen: boolean;
  windowTitle: string;
  closingWindow: boolean;

  /** Apply a pushed {playing,title,url} update. */
  receive: (payload: { playing?: boolean; title?: string; url?: string }) => void;
  /** Apply a pushed {open,title,url} result-window update. */
  receiveWindow: (payload: { open?: boolean; title?: string; url?: string }) => void;
  /** One-shot recovery poll (startup / reload). */
  refresh: () => Promise<void>;
  /** Stop and close the playing window. Optimistic — the push confirms. */
  stop: () => Promise<void>;
  /** Close the kept-open result window. Optimistic — the push confirms. */
  closeWindow: () => Promise<void>;
}

export const useBrowserStore = create<BrowserMediaState>((set) => ({
  playing: false,
  title: '',
  url: '',
  stopping: false,
  windowOpen: false,
  windowTitle: '',
  closingWindow: false,

  receive: (payload) =>
    set({
      playing: !!payload.playing,
      title: payload.playing ? payload.title ?? '' : '',
      url: payload.playing ? payload.url ?? '' : '',
    }),

  receiveWindow: (payload) =>
    set({
      windowOpen: !!payload.open,
      windowTitle: payload.open ? payload.title ?? '' : '',
    }),

  refresh: async () => {
    try {
      const m = await browserApi.getMedia();
      set({
        playing: m.playing,
        title: m.title,
        url: m.url,
        windowOpen: m.window_open,
        windowTitle: m.window_title,
      });
    } catch {
      // Leave the last-known state — a transient failure is not fatal.
    }
  },

  stop: async () => {
    set({ stopping: true });
    try {
      await browserApi.stopMedia();
      set({ playing: false, title: '', url: '' });
    } catch {
      // Keep showing the indicator; the user can retry.
    } finally {
      set({ stopping: false });
    }
  },

  closeWindow: async () => {
    set({ closingWindow: true });
    try {
      await browserApi.closeWindow();
      set({ windowOpen: false, windowTitle: '' });
    } catch {
      // Keep showing the indicator; the user can retry.
    } finally {
      set({ closingWindow: false });
    }
  },
}));
