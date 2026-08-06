import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import LabApp from "./LabApp";
import "../styles/globals.css";

createRoot(document.getElementById("lab-root")!).render(
  <StrictMode>
    <LabApp />
  </StrictMode>,
);
