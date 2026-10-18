import { useEffect, useState, type ReactNode } from "react";
import { loadPlugins } from "./plugins";

/** Loads frontend plugins once the user is authenticated (inside AuthGate), then renders the app. */
export function PluginBoot(props: { children: ReactNode }) {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    loadPlugins().finally(() => setReady(true));
  }, []);
  return ready ? <>{props.children}</> : null;
}
