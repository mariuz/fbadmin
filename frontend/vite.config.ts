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
    chunkSizeWarningLimit: 1500,
  },
});
