import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    // Proxy API and SSE requests to the Python server
    proxy: {
      "/api": "http://localhost:8000",
      "/events": "http://localhost:8000",
      "/health": "http://localhost:8000",
    },
  },
});
