import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import TerminalApp from "./TerminalApp";
import { useUiStore } from "../store/uiStore";
import "../styles/globals.css";

// The shared uiStore boots on the ops console's default ("fund"); this
// bundle is the cockpit, so it opens on the chart.
useUiStore.setState({ view: "terminal" });

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <TerminalApp />
  </StrictMode>,
);
