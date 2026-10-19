import * as React from "react"

/*
 * The theme a chat page shows: the one its visitor switched to, its own, or
 * its visitor's system's; and the document wearing it while the page is open.
 */

const DARK = "(prefers-color-scheme: dark)"

/** The visitor's system's theme, as it changes. */
export function useSystemTheme(): "light" | "dark" {
  return React.useSyncExternalStore(
    (changed) => {
      const query = window.matchMedia(DARK)
      query.addEventListener("change", changed)
      return () => query.removeEventListener("change", changed)
    },
    () => (window.matchMedia(DARK).matches ? "dark" : "light"),
    () => "light"
  )
}

/**
 * Shows the whole document in a theme while it's mounted, so what opens
 * outside the page's region (menus, tooltips) matches it; the theme it had
 * comes back after. Not while `active` is false: a page kept out of sight
 * (the desktop app's chats still replying) leaves it to the one showing.
 */
export function useDocumentTheme(theme: "light" | "dark", active = true) {
  React.useLayoutEffect(() => {
    if (!active) return
    const root = document.documentElement
    const had = ["light", "dark"].filter((name) =>
      root.classList.contains(name)
    )
    root.classList.remove("light", "dark")
    root.classList.add(theme)
    return () => {
      root.classList.remove("light", "dark")
      root.classList.add(...had)
    }
  }, [theme, active])
}

type Theme = "light" | "dark"

const readChoice = (key: string): Theme | undefined => {
  try {
    const kept = localStorage.getItem(key)
    return kept === "light" || kept === "dark" ? kept : undefined
  } catch {
    // Storage blocked (a private window, blocked site data): this visit only.
    return undefined
  }
}

/**
 * The theme a visitor switched a page to, kept in their browser under `key`
 * (one per agent's page), so it's theirs next time; none until they switch.
 */
export function useChosenTheme(
  key: string
): [Theme | undefined, (theme: Theme) => void] {
  const [chosen, setChosen] = React.useState(() => readChoice(key))
  const choose = React.useCallback(
    (theme: Theme) => {
      setChosen(theme)
      try {
        localStorage.setItem(key, theme)
      } catch {
        // Kept for this visit only.
      }
    },
    [key]
  )
  return [chosen, choose]
}
