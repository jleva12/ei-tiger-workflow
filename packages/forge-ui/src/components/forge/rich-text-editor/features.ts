import type { RichTextFeatures, RichTextPreset } from "./types"

const FULL: RichTextFeatures = {
  headings: true,
  textStyle: true,
  alignment: true,
  scripts: true,
  taskLists: true,
  tables: true,
  images: true,
  media: true,
  codeHighlighting: true,
  math: true,
  details: true,
  emoji: true,
  typography: true,
  slashCommands: true,
  dragHandle: true,
  findReplace: true,
  outline: true,
  invisibleCharacters: true,
  fullscreen: true,
  uniqueIds: false,
}

const PRESETS: Record<RichTextPreset, RichTextFeatures> = {
  full: FULL,
  // Documents without layout tools: formatting, lists, tables, code, links.
  standard: {
    ...FULL,
    textStyle: false,
    alignment: false,
    scripts: false,
    media: false,
    math: false,
    details: false,
    dragHandle: false,
    outline: false,
    invisibleCharacters: false,
  },
  // A comment or description box: marks, lists, quotes, code and links.
  minimal: {
    ...FULL,
    headings: false,
    textStyle: false,
    alignment: false,
    scripts: false,
    taskLists: false,
    tables: false,
    images: false,
    media: false,
    codeHighlighting: false,
    math: false,
    details: false,
    slashCommands: false,
    dragHandle: false,
    findReplace: false,
    outline: false,
    invisibleCharacters: false,
    fullscreen: false,
  },
}

export function resolveFeatures(
  preset: RichTextPreset,
  overrides?: Partial<RichTextFeatures>
): RichTextFeatures {
  return { ...PRESETS[preset], ...overrides }
}
