import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Backend proxy: the app talks same-origin (/api, /ws) so it works from any
// host — localhost, a phone on the LAN (`--host`), or a tunnel.
const proxy = {
  "/api": { target: "http://localhost:8000", changeOrigin: true },
  "/ws": { target: "ws://localhost:8000", ws: true },
};

// Perspective's ESM uses top-level await -> esnext target required.
// Two apps, one dev server and one build: `/` is the ops console (broker
// connection + strategy runners) and `/terminal.html` is the discretionary
// options terminal. Separate entries, so the console loads without pulling
// in the chart, the chain, perspective or the Monte-Carlo workers — none of
// which it renders. They share the api client, the stores and the palette.
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
