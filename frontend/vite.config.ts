import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Backend proxy: the app talks same-origin (/api, /ws) so it works from any
// host — localhost, a phone on the LAN (`--host`), or a tunnel.
const proxy = {
  "/api": { target: "http://localhost:8000", changeOrigin: true },
  "/ws": { target: "ws://localhost:8000", ws: true },
};

// Perspective's ESM uses top-level await -> esnext target required.
// Two apps, one dev server and one build: `/` is the options terminal,
// `/lab.html` is the LLM-gated strategy observation deck. Separate entries
// (separate roots, separate stores) so the deck can be open on a second
// screen all night without the terminal's chart/chain work touching it —
// they share only the api client and the palette.
export default defineConfig({
  plugins: [react()],
  build: {
    target: "esnext",
    rollupOptions: { input: { main: "index.html", lab: "lab.html" } },
  },
  // PORT env (when set) wins so parallel dev servers don't fight over 5173.
  server: { port: Number(process.env.PORT) || 5173, strictPort: true, proxy, allowedHosts: true },
  preview: { port: 4173, proxy, allowedHosts: true },
});
