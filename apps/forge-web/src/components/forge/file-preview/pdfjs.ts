import * as pdfjs from "pdfjs-dist"
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url"

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl

/**
 * Where pdf.js finds its CMaps, standard fonts, ICC profiles and wasm
 * decoders: copied into the build by vite.config.ts's pdfjsAssets, under
 * the version they belong to.
 */
const assets = `${import.meta.env.BASE_URL}assets/pdfjs/${pdfjs.version}/`

export const PDF_ASSETS = {
  cMapUrl: `${assets}cmaps/`,
  standardFontDataUrl: `${assets}standard_fonts/`,
  iccUrl: `${assets}iccs/`,
  wasmUrl: `${assets}wasm/`,
}

// The viewer components read the library from a global when their module
// runs, so they're imported only after it's set.
let viewer: Promise<typeof import("pdfjs-dist/web/pdf_viewer.mjs")> | undefined

export function loadPdfViewer() {
  ;(globalThis as { pdfjsLib?: typeof pdfjs }).pdfjsLib ??= pdfjs
  viewer ??= import("pdfjs-dist/web/pdf_viewer.mjs")
  return viewer
}

export { pdfjs }
