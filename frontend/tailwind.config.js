/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bb: {
          black: "#000000",
          bg2: "#0a0a0a",
          panel: "#111111",
          hover: "#1a1a1a",
          amber: "#FFB000",
          orange: "#FFA028",
          muted: "#666666",
          border: "#333333",
          profit: "#00C853",
          loss: "#FF1744",
          neutral: "#2196F3",
        },
      },
      fontFamily: {
        mono: ["SF Mono", "Fira Code", "Consolas", "monospace"],
      },
    },
  },
  corePlugins: {
    // Bloomberg: no rounded corners anywhere.
    borderRadius: false,
  },
  plugins: [],
};
