import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Component tests need a DOM; the pure-function tests don't care. jsdom for
// everything keeps one environment and the suite stays sub-second.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
  },
});
