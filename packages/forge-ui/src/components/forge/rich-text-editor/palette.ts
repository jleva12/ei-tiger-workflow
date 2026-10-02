/*
 * Colours people can give text. Values are the --rte-* variables from
 * rich-text-editor.css, stored as-is so they follow light and dark.
 */

export interface Swatch {
  name: string
  /** A CSS colour, or null for "no colour". */
  value: string | null
}

export const TEXT_COLORS: Swatch[] = [
  { name: "Default", value: null },
  { name: "Gray", value: "var(--rte-text-gray)" },
  { name: "Red", value: "var(--rte-text-red)" },
  { name: "Orange", value: "var(--rte-text-orange)" },
  { name: "Yellow", value: "var(--rte-text-yellow)" },
  { name: "Green", value: "var(--rte-text-green)" },
  { name: "Blue", value: "var(--rte-text-blue)" },
  { name: "Purple", value: "var(--rte-text-purple)" },
  { name: "Pink", value: "var(--rte-text-pink)" },
]

export const HIGHLIGHT_COLORS: Swatch[] = [
  { name: "None", value: null },
  { name: "Gray", value: "var(--rte-highlight-gray)" },
  { name: "Red", value: "var(--rte-highlight-red)" },
  { name: "Orange", value: "var(--rte-highlight-orange)" },
  { name: "Yellow", value: "var(--rte-highlight-yellow)" },
  { name: "Green", value: "var(--rte-highlight-green)" },
  { name: "Blue", value: "var(--rte-highlight-blue)" },
  { name: "Purple", value: "var(--rte-highlight-purple)" },
  { name: "Pink", value: "var(--rte-highlight-pink)" },
]
