import { fileURLToPath } from "node:url"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// In development the agent's server runs beside Vite (uv run main.py): its API
// is sent there. Built, the server serves this UI itself, from the same origin.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
  // The assistant's code highlighting and diagrams are big, and loaded only when a reply has them.
  build: { chunkSizeWarningLimit: 2000 },
})
