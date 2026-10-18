import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies the API to the backend (python -m fbadmin --no-browser).
// Production build is written into the Python package so `fbadmin` serves it.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:5050" },
  },
  build: {
    outDir: "../backend/fbadmin/static",
    emptyOutDir: true,
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      output: {
        // Long-lived vendor chunks: they change rarely, so browsers keep them cached across
        // fbadmin upgrades, and the app chunk stays small. Views that are not needed at
        // start-up (Query Tool, data grid, monitor, processes) are lazy-loaded in App.tsx.
        manualChunks(id: string) {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("sql-formatter")) return "sql-formatter";
          if (id.includes("@codemirror") || id.includes("@lezer") || id.includes("crelt")
            || id.includes("style-mod") || id.includes("w3c-keyname")) return "codemirror";
          if (/node_modules\/(react|react-dom|scheduler)\//.test(id)) return "react";
          return "vendor";
        },
      },
    },
  },
});
