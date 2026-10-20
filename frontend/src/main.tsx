import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./locales";
import "./features";
import App from "./App";
import { AuthGate } from "./features/security/AuthGate";
import { PluginBoot } from "./PluginBoot";
import "./styles.css";
import "./features/mobile/mobile.css"; // phone and tablet layout: after styles.css so it wins
import { installA11y } from "./a11y";
import { installScrollRegions } from "./features/mobile/scrollRegions";

installA11y();
installScrollRegions();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AuthGate>
      <PluginBoot>
        <App />
      </PluginBoot>
    </AuthGate>
  </StrictMode>,
);
