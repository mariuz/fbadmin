import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./locales";
import "./features";
import App from "./App";
import { AuthGate } from "./features/security/AuthGate";
import { PluginBoot } from "./PluginBoot";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AuthGate>
      <PluginBoot>
        <App />
      </PluginBoot>
    </AuthGate>
  </StrictMode>,
);
