/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The console is served by the TerraShield API process under /console, so the
// build is rooted there. In development Vite proxies /api to a local
// `terrashield serve`, which keeps the browser on one origin exactly as in
// production: no CORS, no credentials crossing origins.
export default defineConfig({
  base: "/console/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: process.env.TERRASHIELD_API ?? "http://127.0.0.1:8787", changeOrigin: false },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    // Everything ships inside the bundle: the console must open in a facility
    // with no route to the internet, so there is no CDN and no web font.
    assetsInlineLimit: 0,
    chunkSizeWarningLimit: 1500,
  },
  test: { environment: "node" },
});
