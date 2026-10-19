import type {
  EditorFeatures,
  EditorPreset,
  ResolvedEditorFeatures,
} from "../types.ts"

const FULL: ResolvedEditorFeatures = {
  bold: true,
  italic: true,
  underline: true,
  strike: true,
  inlineCode: true,
  highlight: true,
  subscript: true,
  superscript: true,
  links: true,
  headings: true,
  blockquote: true,
  orderedList: true,
  bulletList: true,
  taskList: true,
  codeBlock: true,
  codeHighlighting: true,
  horizontalRule: true,
  tables: true,
  images: true,
  math: true,
  history: true,
  markdownShortcuts: true,
  slashCommands: true,
  characterCount: true,
  emoji: true,
  typography: true,
  dragHandle: true,
  findReplace: true,
  outline: true,
  invisibleCharacters: true,
  fullscreen: true,
}

const PRESETS: Record<EditorPreset, ResolvedEditorFeatures> = {
  full: FULL,
  // Basic formatting, headings, lists, links, quotes, code and history; the
  // advanced blocks and the tools that take over browser keys are opt-in.
  standard: {
    ...FULL,
    highlight: false,
    subscript: false,
    superscript: false,
    taskList: false,
    tables: false,
    images: false,
    math: false,
    slashCommands: false,
    characterCount: false,
    emoji: false,
    typography: false,
    dragHandle: false,
    findReplace: false,
    outline: false,
    invisibleCharacters: false,
    fullscreen: false,
  },
  // A comment or description box: marks, lists, quotes and links.
  minimal: {
    ...FULL,
    highlight: false,
    subscript: false,
    superscript: false,
    headings: false,
    taskList: false,
    codeBlock: false,
    codeHighlighting: false,
    horizontalRule: false,
    tables: false,
    images: false,
    math: false,
    slashCommands: false,
    characterCount: false,
    emoji: false,
    typography: false,
    dragHandle: false,
    findReplace: false,
    outline: false,
    invisibleCharacters: false,
    fullscreen: false,
  },
}

/** The preset with `overrides` applied; dependent features follow their base. */
export function resolveFeatures(
  preset: EditorPreset = "standard",
  overrides?: EditorFeatures
): ResolvedEditorFeatures {
  const features = { ...PRESETS[preset] }
  for (const [key, value] of Object.entries(overrides ?? {})) {
    if (typeof value === "boolean") {
      features[key as keyof ResolvedEditorFeatures] = value
    }
  }
  // Highlighting and the outline need what they decorate.
  features.codeHighlighting &&= features.codeBlock
  features.outline &&= features.headings
  return features
}
