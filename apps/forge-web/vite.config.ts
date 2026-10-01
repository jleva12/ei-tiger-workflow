import path from "path"
import tailwindcss from "@tailwindcss/vite"
import { tanstackRouter } from "@tanstack/router-plugin/vite"
import react from "@vitejs/plugin-react"
import { defineConfig, loadEnv } from "vite"

// The admin API the dev server forwards /api and /health to, so the console
// can reach it at a relative VITE_API_URL (/api/v1) from wherever the page is
// served: localhost, or a tunnel such as ngrok from another device.
const apiTarget =
  loadEnv("development", __dirname, "").API_PROXY_TARGET ||
  "http://localhost:8101"

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    // Generates src/routeTree.gen.ts from src/routes; must run before react().
    tanstackRouter({ target: "react", autoCodeSplitting: true }),
    react(),
    tailwindcss(),
  ],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5190,
    strictPort: true,
    // ngrok tunnels to the dev server; free ngrok hosts change each restart.
    allowedHosts: [".ngrok-free.app"],
    // Anchored, so a page route like /api-keys stays the console's.
    proxy: {
      "^/api/": { target: apiTarget, changeOrigin: true },
      "^/health(/|$)": { target: apiTarget, changeOrigin: true },
    },
  },
})
