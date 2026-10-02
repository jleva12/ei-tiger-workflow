import * as React from "react"
import { useNavigate, useSearch } from "@tanstack/react-router"

import { PreferencesPanel } from "@/components/forge/preferences"
import { NavItem } from "@/components/forge/workspace-sidebar"
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  isSettingsSection,
  SETTINGS_SECTION_IDS,
  SETTINGS_SECTIONS,
  type SettingsSection,
} from "@/app/settings/settings"
import { AssistantPlacementRow } from "./assistant-placement-row"

/**
 * Settings, in a dialog over whatever page you're on: its sections listed
 * down the side, the open one's name in a breadcrumb above its content.
 * It's open while the URL has `?settings=<section>`, so the rail's Settings
 * button, your account menu and links all open it, and Back closes it.
 * Narrow screens trade the side list for tabs under the title. Render it
 * once, in the app shell.
 */
export function SettingsDialog() {
  const navigate = useNavigate()
  const open = useSearch({
    from: "__root__",
    select: (search) => search.settings,
  })
  // Keep showing the last section while the dialog animates out.
  const [section, setSection] = React.useState<SettingsSection>(
    open ?? "display"
  )
  if (open && open !== section) setSection(open)
  const { label, description } = SETTINGS_SECTIONS[section]

  function show(next: SettingsSection | undefined) {
    void navigate({
      to: ".",
      search: (prev) => ({ ...prev, settings: next }),
      // Switching sections doesn't add to history; Back closes Settings.
      replace: true,
    })
  }

  return (
    <Dialog
      open={open !== undefined}
      onOpenChange={(next) => !next && show(undefined)}
    >
      <DialogContent className="flex h-[min(42rem,calc(100dvh-4rem))] gap-0 overflow-hidden p-0 sm:max-w-[min(72rem,calc(100%-4rem))]">
        <DialogTitle className="sr-only">Settings</DialogTitle>
        <DialogDescription className="sr-only">
          Your display preferences.
        </DialogDescription>
        <nav
          aria-label="Settings"
          className="hidden w-60 shrink-0 flex-col gap-0.5 border-r bg-sidebar p-2 md:flex"
        >
          {SETTINGS_SECTION_IDS.map((id) => (
            <NavItem
              key={id}
              icon={SETTINGS_SECTIONS[id].icon}
              active={id === section}
              onClick={() => show(id)}
            >
              {SETTINGS_SECTIONS[id].label}
            </NavItem>
          ))}
        </nav>
        <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
          {/* Room on the right for the dialog's close button. */}
          <header className="flex h-14 shrink-0 items-center pr-14 pl-5">
            {/* Narrow, the tabs below name the section. */}
            <Breadcrumb>
              <BreadcrumbList>
                <BreadcrumbItem className="max-md:text-sm max-md:font-medium max-md:text-foreground">
                  Settings
                </BreadcrumbItem>
                <BreadcrumbSeparator className="max-md:hidden" />
                <BreadcrumbItem className="max-md:hidden">
                  <BreadcrumbPage>{label}</BreadcrumbPage>
                </BreadcrumbItem>
              </BreadcrumbList>
            </Breadcrumb>
          </header>
          <Tabs
            value={section}
            onValueChange={(value) => isSettingsSection(value) && show(value)}
            className="shrink-0 border-b px-5 md:hidden"
          >
            <TabsList variant="line" aria-label="Settings">
              {SETTINGS_SECTION_IDS.map((id) => (
                <TabsTrigger key={id} value={id}>
                  {SETTINGS_SECTIONS[id].label}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
          <div
            tabIndex={0}
            className="min-h-0 flex-1 overflow-y-auto px-5 pt-1 pb-6 max-md:pt-4"
          >
            <p className="mb-5 text-xs text-muted-foreground">{description}</p>
            <PreferencesPanel>
              <AssistantPlacementRow />
            </PreferencesPanel>
          </div>
        </main>
      </DialogContent>
    </Dialog>
  )
}
