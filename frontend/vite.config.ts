import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Backend proxy: the app talks same-origin (/api) so it works from any
// host — localhost, a phone on the LAN (`--host`), or a tunnel. The console
// polls REST; there is no WebSocket.
const proxy = {
  "/api": { target: "http://localhost:8000", changeOrigin: true },
};

// One app: the ops console. The discretionary options terminal was retired
// on 2026-08-07 — the product is a portal for running strategies, and the
// chart, chain, payoff designer, Monte-Carlo workers and perspective grid
// went with it. esnext is kept because the toolchain still targets it.
export default defineConfig({
  plugins: [react()],
  build: { target: "esnext" },
  // PORT env (when set) wins so parallel dev servers don't fight over 5173.
  server: { port: Number(process.env.PORT) || 5173, strictPort: true, proxy, allowedHosts: true },
  preview: { port: 4173, proxy, allowedHosts: true },
});
