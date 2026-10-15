/*
 * A chat page's settings: its words, what it shows and its look, as its
 * builder sets them (the configuration page) and the page reads them
 * (`ChatPageInfo`). The admin API's `chat_agents.page_settings` keeps them.
 */

export type ChatPageTheme = "system" | "light" | "dark"
/** How the visitor's messages are drawn: in grey, in the accent, or tinted with it. */
export type ChatPageBubbles = "neutral" | "accent" | "tint"
export type ChatPageCorners = "default" | "rounded" | "square"
export type ChatPageFont = "default" | "system" | "serif" | "mono"
export type ChatPageWidth = "default" | "narrow" | "wide"

export type ChatPageSettings = {
  /** The prompts an empty conversation offers under the message box. */
  suggestions: string[]
  /** It leaves out the visitor's list of conversations. */
  hide_history: boolean
  /** It leaves out its top bar. */
  hide_top_bar: boolean
  /** It suggests follow-up questions after each reply. */
  follow_ups: boolean
  /** The name over the page and in the browser's tab; "" for the agent's. */
  title: string
  /** The welcome's heading; "" for "Talk to <name>". */
  welcome_title: string
  /** The welcome's message; "" for the agent's description. */
  welcome_message: string
  /** The message box's placeholder; "" for "Send a message...". */
  placeholder: string
  /** A note under the message box; "" for none. */
  footer_note: string
  theme: ChatPageTheme
  /** A button on the page that switches it between light and dark, for each visitor. */
  theme_toggle: boolean
  /** `#rrggbb`; null for Forge's ink. */
  accent: string | null
  bubbles: ChatPageBubbles
  corners: ChatPageCorners
  font: ChatPageFont
  width: ChatPageWidth
}

export const PAGE_DEFAULTS: ChatPageSettings = {
  suggestions: [],
  hide_history: false,
  hide_top_bar: false,
  follow_ups: true,
  title: "",
  welcome_title: "",
  welcome_message: "",
  placeholder: "",
  footer_note: "",
  theme: "light",
  theme_toggle: true,
  accent: null,
  bubbles: "neutral",
  corners: "default",
  font: "default",
  width: "default",
}

/** The most prompts a chat page offers. */
export const MAX_PAGE_SUGGESTIONS = 6

/** The most characters each text takes, as the admin API checks them. */
export const PAGE_TEXT_LIMITS = {
  title: 80,
  welcome_title: 120,
  welcome_message: 600,
  placeholder: 120,
  footer_note: 300,
} as const satisfies Partial<Record<keyof ChatPageSettings, number>>

const KEYS = Object.keys(PAGE_DEFAULTS) as (keyof ChatPageSettings)[]

/** A page's settings, each it doesn't have (an older page, server) at its default. */
export function pageSettingsOf(
  page: Partial<ChatPageSettings> | null | undefined
): ChatPageSettings {
  const read = { ...PAGE_DEFAULTS } as Record<string, unknown>
  for (const key of KEYS) {
    const value = page?.[key]
    if (value !== undefined) read[key] = value
  }
  return read as ChatPageSettings
}

/** The settings that differ: what saving sends. */
export function changedSettings(
  saved: ChatPageSettings,
  next: ChatPageSettings
): Partial<ChatPageSettings> {
  const changed: Record<string, unknown> = {}
  for (const key of KEYS) {
    const a = saved[key]
    const b = next[key]
    const same =
      Array.isArray(a) && Array.isArray(b)
        ? a.length === b.length && a.every((value, index) => value === b[index])
        : a === b
    if (!same) changed[key] = b
  }
  return changed as Partial<ChatPageSettings>
}

/** Accents a page offers at a click; any other is picked or typed. */
export const ACCENTS: { name: string; value: string | null }[] = [
  { name: "Forge ink", value: null },
  { name: "Blue", value: "#2563eb" },
  { name: "Indigo", value: "#4f46e5" },
  { name: "Violet", value: "#7c3aed" },
  { name: "Teal", value: "#0d9488" },
  { name: "Green", value: "#15803d" },
  { name: "Amber", value: "#d97706" },
  { name: "Rose", value: "#e11d48" },
  { name: "Slate", value: "#475569" },
]

export const isAccent = (value: string) => /^#[0-9a-f]{6}$/i.test(value)

/** sRGB → relative luminance (WCAG). */
function luminance(hex: string) {
  const channel = (at: number) => {
    const c = parseInt(hex.slice(at, at + 2), 16) / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5)
}

const contrast = (a: number, b: number) =>
  (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)

/** The text on an accent: white or near-black, whichever reads better. */
export function accentInk(hex: string) {
  const l = luminance(hex)
  return contrast(l, 1) >= contrast(l, luminance("#15171c"))
    ? "#ffffff"
    : "#15171c"
}

/** The theme a page shows: its own, or the visitor's system's. */
export const resolvedTheme = (
  theme: ChatPageTheme,
  system: "light" | "dark"
): "light" | "dark" => (theme === "system" ? system : theme)

/** The name a page goes by: its own, or its agent's. */
export const pageTitleOf = (
  settings: Pick<ChatPageSettings, "title">,
  agent: { name: string }
) => settings.title.trim() || agent.name || "Agent"

/** Prompts as they're saved: trimmed, without blanks, each once. */
export const cleanedPrompts = (rows: readonly string[]) => [
  ...new Set(rows.map((row) => row.trim()).filter(Boolean)),
]
