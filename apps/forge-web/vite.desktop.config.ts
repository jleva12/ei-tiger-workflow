import path from "path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig, loadEnv } from "vite"

/*
 * The desktop app's screen (src/desktop, desktop.html): the console's chat
 * pages, built on their own into dist-desktop/ for apps/forge-desktop's
 * Tauri window. It shares the console's source and node_modules, so the
 * desktop draws a chat page exactly as the web does.
 *
 *   npm run dev:desktop     # http://localhost:5195/desktop.html (Tauri's devUrl)
 *   npm run build:desktop   # dist-desktop/
 */

const PORT = 5195

export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, __dirname, "")
  // The server new agents are on unless the person names another. Unset in
  // development: the admin API the console's dev server forwards to.
  const server =
    env.VITE_DESKTOP_API_URL ||
    (command === "serve"
      ? `${(env.API_PROXY_TARGET || "http://localhost:8101").replace(/\/+$/, "")}/api/v1`
      : "")
  // Who the app signs in as until single sign-on hands it a token: the dev
  // server takes the console's development token (make web-token); a build
  // only one it's given, so no installer carries a person's token.
  const token =
    env.VITE_DESKTOP_API_TOKEN ||
    (command === "serve" ? env.VITE_API_TOKEN || "" : "")
  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
    define: {
      "import.meta.env.VITE_DESKTOP_API_URL": JSON.stringify(server),
      "import.meta.env.VITE_DESKTOP_API_TOKEN": JSON.stringify(token),
      // The console's own client isn't the app's, and its token stays out.
      "import.meta.env.VITE_API_TOKEN": JSON.stringify(""),
    },
    // Tauri reads its errors from the terminal; keep Vite's there too.
    clearScreen: false,
    server: {
      port: PORT,
      strictPort: true,
    },
    build: {
      outDir: "dist-desktop",
      emptyOutDir: true,
      rolldownOptions: {
        input: path.resolve(__dirname, "desktop.html"),
      },
    },
  }
})
