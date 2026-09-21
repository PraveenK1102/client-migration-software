import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Localhost-only single-user prototype. The dev server proxies /api to the backend
// so the browser never talks to Groq directly and no key is exposed to the frontend.
export default defineConfig({
  plugins: [react()],
  server: {
    // Honor an assigned PORT (e.g. from the preview harness / autoPort) so the dev server can run on
    // a free port when 5173 is taken; falls back to 5173 for the plain `npm run dev` case.
    port: Number(process.env.PORT) || 5173,
    proxy: {
      "/api": {
        target: process.env.BACKEND_URL || "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
