/// <reference types="vite/client" />

// Ambient types for Vite's client-side surface — `import.meta.env`, and the
// side-effect module declarations for `*.css` / `*.svg` / `?raw` imports that
// `noUncheckedSideEffectImports` (enabled in tsconfig.json) requires. Without
// this line, `import './index.css'` in main.tsx fails typecheck.

// Type your own env vars by uncommenting and extending. Vite only exposes
// variables prefixed with `VITE_` to client code.
//
// interface ImportMetaEnv {
//   readonly VITE_API_URL: string
// }
//
// interface ImportMeta {
//   readonly env: ImportMetaEnv
// }
