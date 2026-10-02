const IS_MAC =
  typeof navigator !== "undefined" &&
  /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent)

const MAC_KEYS: Record<string, string> = {
  Mod: "⌘",
  Alt: "⌥",
  Shift: "⇧",
  Ctrl: "⌃",
}

const OTHER_KEYS: Record<string, string> = {
  Mod: "Ctrl",
  Alt: "Alt",
  Shift: "Shift",
  Ctrl: "Ctrl",
}

/**
 * Turns a Tiptap key binding (`Mod-Shift-x`) into what the person presses:
 * `⌘⇧X` on Apple devices, `Ctrl+Shift+X` elsewhere.
 */
export function formatShortcut(binding: string) {
  const keys = IS_MAC ? MAC_KEYS : OTHER_KEYS
  const parts = binding
    .split("-")
    .map(
      (part) => keys[part] ?? (part.length === 1 ? part.toUpperCase() : part)
    )
  return parts.join(IS_MAC ? "" : "+")
}
