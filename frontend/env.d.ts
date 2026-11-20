/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "true" shows the demo accounts on the login page in a production build too (always shown in `pnpm dev`). */
  readonly VITE_DEMO_ACCOUNTS?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
