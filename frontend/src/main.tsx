import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./features";
import App from "./App";
import { AuthGate } from "./features/security/AuthGate";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AuthGate>
      <App />
    </AuthGate>
  </StrictMode>,
);
