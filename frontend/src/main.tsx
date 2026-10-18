import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./features";
import App from "./App";
import { loadPlugins } from "./plugins";
import "./styles.css";

loadPlugins().finally(() => {
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
});
