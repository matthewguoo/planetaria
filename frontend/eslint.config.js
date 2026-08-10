// Correctness-focused lint, mirroring backend ruff's philosophy (F + E9
// only): typescript-eslint recommended for real errors, react-hooks for the
// dependency-array and unmount-race class of bug. Style stays out.
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";

export default tseslint.config(
  { ignores: ["dist", "node_modules"] },
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // The api layer casts axios payloads by declaration; that is the
      // documented trust boundary until response_model + codegen land.
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
    },
  },
);
