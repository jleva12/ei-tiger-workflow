import { useSyncExternalStore } from "react"

import type { DataTableDensity } from "./types"

// UserPreferencesProvider sets data-forge-density on <html>. Reading the
// attribute keeps the table independent of the preferences store: it follows
// the user's density whenever something sets it, and is comfortable otherwise.
const attribute = "data-forge-density"

function subscribe(notify: () => void) {
  const observer = new MutationObserver(notify)
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: [attribute],
  })
  return () => observer.disconnect()
}

const getSnapshot = (): DataTableDensity =>
  document.documentElement.getAttribute(attribute) === "compact"
    ? "compact"
    : "default"

const getServerSnapshot = (): DataTableDensity => "default"

/** The user's density preference, as a table density; updates live. */
export function usePreferredDensity(): DataTableDensity {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot)
}
