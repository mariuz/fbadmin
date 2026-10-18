// Bundled translations. Each catalog maps the English source string to its translation;
// empty strings fall back to English.
import { registerCatalog } from "../i18n";

const catalogs = import.meta.glob<{ default: Record<string, string> }>("./*.json", { eager: true });
for (const [path, mod] of Object.entries(catalogs)) {
  const code = path.replace(/^\.\/|\.json$/g, "");
  const { "@name": name, ...messages } = mod.default;
  const filled = Object.fromEntries(Object.entries(messages).filter(([, v]) => v));
  registerCatalog(code, name || code, filled);
}
