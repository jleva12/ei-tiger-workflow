import * as React from "react"
import { isTauri } from "@tauri-apps/api/core"
import { getCurrent, onOpenUrl } from "@tauri-apps/plugin-deep-link"

import {
  parseDesktopLink,
  type DesktopAgentLink,
} from "@/features/agents/lib/desktop-link"

/*
 * The app's own links reaching its screen: the one it was started with,
 * and each opened while it runs (the shell brings the window forward). Only
 * links of the right shape get through (`parseDesktopLink`); the screen
 * still asks before adding an agent, since any website can open one.
 */

// Each link once per window, though its effect runs twice (StrictMode)
// and the app keeps answering getCurrent with the one it started with.
const seen = new Set<string>()

/** Calls `onLink` with each agent a link to the app offers. */
export function useDesktopLinks(onLink: (link: DesktopAgentLink) => void) {
  const latest = React.useRef(onLink)
  React.useEffect(() => {
    latest.current = onLink
  }, [onLink])

  React.useEffect(() => {
    if (!isTauri()) return
    const take = (urls: string[] | null) => {
      for (const url of urls ?? []) {
        if (seen.has(url)) continue
        seen.add(url)
        const link = parseDesktopLink(url)
        if (link) latest.current(link)
        else console.warn("Not a link to an agent:", url)
      }
    }
    let current = true
    let stop: (() => void) | undefined
    getCurrent()
      .then((urls) => current && take(urls))
      .catch((error: unknown) => console.warn("No start link:", error))
    onOpenUrl((urls) => take(urls))
      .then((unlisten) => {
        if (current) stop = unlisten
        else unlisten()
      })
      .catch((error: unknown) => console.warn("No app links:", error))
    return () => {
      current = false
      stop?.()
    }
  }, [])
}
