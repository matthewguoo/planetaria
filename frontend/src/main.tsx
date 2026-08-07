import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import OpsApp from "./OpsApp";
import "./styles/globals.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <OpsApp />
  </StrictMode>,
);
