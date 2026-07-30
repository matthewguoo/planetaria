import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Backend proxy: the app talks same-origin (/api, /ws) so it works from any
// host — localhost, a phone on the LAN (`--host`), or a tunnel.
const proxy = {
  "/api": { target: "http://localhost:8000", changeOrigin: true },
  "/ws": { target: "ws://localhost:8000", ws: true },
};

// Perspective's ESM uses top-level await -> esnext target required.
export default defineConfig({
  plugins: [react()],
  build: { target: "esnext" },
  // PORT env (when set) wins so parallel dev servers don't fight over 5173.
  server: { port: Number(process.env.PORT) || 5173, strictPort: true, proxy, allowedHosts: true },
  preview: { port: 4173, proxy, allowedHosts: true },
});
