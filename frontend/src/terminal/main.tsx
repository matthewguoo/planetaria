import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import TerminalApp from "./TerminalApp";
import { useUiStore } from "../store/uiStore";
import "../styles/globals.css";

// The shared uiStore boots on the ops console's default ("fund"); this
// bundle is the cockpit, so it opens on the chart — except at the root
// path (the live server serves this bundle at "/") or ?view=overview,
// where it opens on the ACCOUNT OVERVIEW: holdings, equity, protection.
const params = new URLSearchParams(window.location.search);
const bootOverview = window.location.pathname === "/" || params.get("view") === "overview";
useUiStore.setState({ view: bootOverview ? "overview" : "terminal" });

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <TerminalApp />
  </StrictMode>,
);
