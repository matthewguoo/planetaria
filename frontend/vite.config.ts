import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Backend proxy: the app talks same-origin (/api, /ws) so it works from any
// host — localhost, a phone on the LAN (`--host`), or a tunnel. BACKEND_URL
// points a dev server at the isolated LIVE backend instead (:8001) — e.g.
// `$env:BACKEND_URL="http://localhost:8001"; $env:PORT=5174; npm run dev`.
// The UI itself is mode-aware at runtime (Account.mode), so one build
// serves both servers.
const backend = process.env.BACKEND_URL || "http://localhost:8000";
const proxy = {
  "/api": { target: backend, changeOrigin: true },
  "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
};

// Two apps, one dev server and one build: `/` is the ops console (broker
// connection + strategy runners) and `/terminal.html` is the discretionary
// trading terminal (options designer + equity swing ticket, resurrected
// 2026-08-31). Separate entries, so the console loads without pulling in the
// chart, the chain or the Monte-Carlo workers — none of which it renders.
// They share the api client, the stores and the palette. In production the
// backend serves `dist/` itself (StaticFiles in app/main.py), so
// `npm run build` is the whole deploy.
export default defineConfig({
  plugins: [react()],
  build: {
    target: "esnext",
    rollupOptions: { input: { main: "index.html", terminal: "terminal.html" } },
  },
  // PORT env (when set) wins so parallel dev servers don't fight over 5173.
  server: { port: Number(process.env.PORT) || 5173, strictPort: true, proxy, allowedHosts: true },
  preview: { port: 4173, proxy, allowedHosts: true },
});
