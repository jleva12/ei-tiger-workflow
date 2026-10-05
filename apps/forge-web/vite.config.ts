import fs from "fs"
import path from "path"
import tailwindcss from "@tailwindcss/vite"
import { tanstackRouter } from "@tanstack/router-plugin/vite"
import react from "@vitejs/plugin-react"
import { defineConfig, loadEnv, type Plugin } from "vite"

// The admin API the dev server forwards /api and /health to, so the console
// can reach it at a relative VITE_API_URL (/api/v1) from wherever the page is
// served: localhost, or a tunnel such as ngrok from another device.
const apiTarget =
  loadEnv("development", __dirname, "").API_PROXY_TARGET ||
  "http://localhost:8101"

/**
 * pdf.js reads its CMaps (CJK text), standard fonts, ICC profiles and wasm
 * image decoders from URLs at runtime, by their own names. This serves them
 * from node_modules in development and copies them into the build under
 * assets/pdfjs/<version>/, where the web server caches them for good; the
 * file viewer points pdf.js there (components/forge/file-preview/pdfjs.ts).
 */
function pdfjsAssets(): Plugin {
  const root = path.resolve(__dirname, "node_modules/pdfjs-dist")
  const { version } = JSON.parse(
    fs.readFileSync(path.join(root, "package.json"), "utf8")
  ) as { version: string }
  const base = `assets/pdfjs/${version}`
  const folders = ["cmaps", "standard_fonts", "iccs", "wasm"]
  const files = () =>
    folders.flatMap((folder) =>
      fs
        .readdirSync(path.join(root, folder))
        .filter((name) => !name.startsWith("LICENSE"))
        .map((name) => `${folder}/${name}`)
    )
  return {
    name: "forge-pdfjs-assets",
    configureServer(server) {
      const known = new Set(files())
      server.middlewares.use(`/${base}/`, (request, response, next) => {
        const file = decodeURIComponent(
          (request.url ?? "").split("?")[0]
        ).replace(/^\/+/, "")
        if (!known.has(file)) return next()
        response.setHeader(
          "Content-Type",
          file.endsWith(".wasm")
            ? "application/wasm"
            : file.endsWith(".js")
              ? "text/javascript"
              : "application/octet-stream"
        )
        fs.createReadStream(path.join(root, file)).pipe(response)
      })
    },
    generateBundle() {
      for (const file of files())
        this.emitFile({
          type: "asset",
          fileName: `${base}/${file}`,
          source: fs.readFileSync(path.join(root, file)),
        })
    },
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    // Generates src/routeTree.gen.ts from src/routes; must run before react().
    tanstackRouter({ target: "react", autoCodeSplitting: true }),
    react(),
    tailwindcss(),
    pdfjsAssets(),
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
