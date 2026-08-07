import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import TerminalApp from "./TerminalApp";
import "../styles/globals.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <TerminalApp />
  </StrictMode>,
);
