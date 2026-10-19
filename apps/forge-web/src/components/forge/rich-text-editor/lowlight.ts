import dockerfile from "highlight.js/lib/languages/dockerfile"
import { common, createLowlight } from "lowlight"

/** Highlight.js grammars for code blocks: the common set plus Dockerfiles. */
export const lowlight = createLowlight({ ...common, dockerfile })
