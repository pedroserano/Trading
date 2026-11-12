import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";

// Opened from a home-screen icon: iOS dims a band at the top of the screen, deeper than the
// status bar, so the header starts lower (styles.css, html.standalone).
const nav = window.navigator as Navigator & { standalone?: boolean };
if (nav.standalone || window.matchMedia("(display-mode: standalone)").matches) {
  document.documentElement.classList.add("standalone");
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
