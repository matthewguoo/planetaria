import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Perspective's ESM uses top-level await -> esnext target required.
export default defineConfig({
  plugins: [react()],
  build: { target: "esnext" },
  server: { port: 5173, strictPort: true },
});
