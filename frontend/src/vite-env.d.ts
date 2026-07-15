/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Plain-browser dev only: the API auth token (contents of
   *  ~/.jarvis/auth_token), set in frontend/.env.local. In Electron the
   *  token arrives via window.__JARVIS_TOKEN__ instead. */
  readonly VITE_JARVIS_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
