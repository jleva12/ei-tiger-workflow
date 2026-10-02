import * as React from "react"
import { useNavigate } from "@tanstack/react-router"

import type { IconProp } from "@/components/forge/icons"

/**
 * Settings' sections, as its dialog lists them. Settings opens over any page
 * with `?settings=<section>`, so it's linkable and Back closes it.
 */
export const SETTINGS_SECTIONS = {
  display: {
    label: "Display",
    icon: "sun",
    description:
      "Theme, density, text size and where the assistant opens. Saved in this browser.",
  },
} as const satisfies Record<
  string,
  { label: string; icon: IconProp; description: string }
>

export type SettingsSection = keyof typeof SETTINGS_SECTIONS

export const SETTINGS_SECTION_IDS = Object.keys(
  SETTINGS_SECTIONS
) as SettingsSection[]

export const isSettingsSection = (value: unknown): value is SettingsSection =>
  typeof value === "string" && Object.hasOwn(SETTINGS_SECTIONS, value)

/** The search param every route takes: the Settings section open, if any. */
export type SettingsSearch = { settings?: SettingsSection }

/**
 * Opens Settings over the page you're on, at a section (Display unless
 * said), keeping the page's own search. Back closes it again.
 */
export function useOpenSettings() {
  const navigate = useNavigate()
  return React.useCallback(
    (section: SettingsSection = "display") =>
      void navigate({
        to: ".",
        search: (prev) => ({ ...prev, settings: section }),
      }),
    [navigate]
  )
}
