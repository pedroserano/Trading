import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development, run the backend on :8000 and open the Vite server; API calls are proxied.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/login": "http://127.0.0.1:8000",
      "/connect": "http://127.0.0.1:8000",
    },
  },
});
