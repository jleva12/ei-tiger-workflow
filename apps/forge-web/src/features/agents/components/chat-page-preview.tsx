import * as React from "react"
import { ComputerIcon, SmartPhone01Icon } from "@hugeicons/core-free-icons"

import { Icon } from "@/components/forge/icon"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { cn } from "@/lib/utils"
import { useMe } from "@/lib/users"
import { ChatPageView } from "@/features/agents/components/chat-page-view"
import { RUNTIME_URL, type ChatAgentRecord } from "@/features/agents/lib/api"
import {
  chatPageDesktopLink,
  chatPageLink,
} from "@/features/agents/lib/chat-page"
import { agentNamesOf, workflowNamesOf } from "@/features/agents/lib/document"
import type { ChatPageSettings } from "@/features/agents/lib/page-settings"
import { useSystemTheme } from "@/features/agents/lib/page-theme"
import { consoleHeaders } from "@/features/agents/lib/runtime-headers"

type Device = "desktop" | "mobile"

/** A small segmented switch over the preview. */
function PreviewSwitch<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: T
  options: {
    value: T
    label: string
    icon?: React.ComponentProps<typeof Icon>["icon"]
  }[]
  onChange: (value: T) => void
}) {
  return (
    <ToggleGroup
      aria-label={label}
      value={[value]}
      onValueChange={(next) => {
        if (next[0]) onChange(next[0] as T)
      }}
      spacing={0}
      className="rounded-(--radius-control) bg-muted p-[3px]"
    >
      {options.map((option) => (
        <ToggleGroupItem
          key={option.value}
          value={option.value}
          aria-label={option.label}
          className="h-[24px] min-w-0 gap-1.5 rounded-(--radius-soft)! px-2 text-2xs font-normal text-muted-foreground hover:bg-transparent hover:text-foreground aria-pressed:bg-background aria-pressed:text-foreground aria-pressed:shadow-(--shadow-raised)"
        >
          {option.icon && <Icon icon={option.icon} size={13} />}
          {option.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  )
}

/**
 * The chat page as the people it's shared with will see it, in the
 * settings being made (saved or not): live, talking to the version the page
 * runs (its draft before it's published) as the signed-in person. On a
 * desktop or a phone's width; in either theme a visitor could see (their
 * system's, or the one they switch to with the page's theme button).
 */
export function ChatPagePreview({
  record,
  settings,
  token,
}: {
  record: ChatAgentRecord
  settings: ChatPageSettings
  /** The page's link, once it has one. */
  token: string | undefined
}) {
  const me = useMe()
  const userId = me.data?.subject
  const published = record.published_version !== null
  const appName = published ? record.id : `${record.id}@draft`
  const connection = React.useMemo(
    () =>
      RUNTIME_URL && userId
        ? { url: RUNTIME_URL, appName, userId, headers: consoleHeaders }
        : undefined,
    [appName, userId]
  )
  const document = record.document
  const agent = React.useMemo(
    () => ({
      name: document.name,
      description: document.description,
      agents: agentNamesOf(document),
      workflows: workflowNamesOf(document),
    }),
    [document]
  )
  const [device, setDevice] = React.useState<Device>("desktop")
  const system = useSystemTheme()
  // The theme a visitor would see: switched to with the page's theme
  // button (or the switch over the preview), or what their system shows,
  // for a page that follows it. A change of the page's theme starts afresh.
  const [picked, setPicked] = React.useState<{
    for: ChatPageSettings["theme"]
    theme: "light" | "dark"
  }>()
  const canPick = settings.theme === "system" || settings.theme_toggle
  const pick = React.useCallback(
    (theme: "light" | "dark") => setPicked({ for: settings.theme, theme }),
    [settings.theme]
  )
  const theme =
    (canPick && picked?.for === settings.theme ? picked.theme : undefined) ??
    (settings.theme === "system" ? system : settings.theme)

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 p-4">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="flex min-w-0 flex-1 flex-col">
          <h2 className="text-xs font-semibold text-foreground">Preview</h2>
          <p className="truncate text-2xs text-muted-foreground">
            {published
              ? `As visitors see it, talking to v${record.published_version} as you.`
              : "Not published yet: it talks to the draft, as you."}
          </p>
        </div>
        {canPick && (
          <PreviewSwitch
            label={
              settings.theme_toggle
                ? "The theme a visitor switched to"
                : "The visitor's system theme"
            }
            value={theme}
            options={[
              { value: "light", label: "Light" },
              { value: "dark", label: "Dark" },
            ]}
            onChange={pick}
          />
        )}
        <PreviewSwitch
          label="Device"
          value={device}
          options={[
            { value: "desktop", label: "Desktop", icon: ComputerIcon },
            { value: "mobile", label: "Phone", icon: SmartPhone01Icon },
          ]}
          onChange={setDevice}
        />
      </div>
      <div className="flex min-h-0 flex-1 justify-center">
        <div
          data-slot="chat-page-preview"
          className={cn(
            "flex h-full min-h-0 flex-col overflow-hidden border bg-background shadow-(--shadow-float) transition-[width] duration-200",
            device === "mobile"
              ? "w-[390px] max-w-full rounded-[1.75rem] border-4 border-foreground/80"
              : "w-full rounded-(--radius-dialog)"
          )}
        >
          {device === "desktop" && (
            <div className="flex h-8 shrink-0 items-center gap-3 border-b bg-muted/60 px-3">
              <span className="flex gap-1.5" aria-hidden="true">
                <span className="size-2.5 rounded-full bg-foreground/15" />
                <span className="size-2.5 rounded-full bg-foreground/15" />
                <span className="size-2.5 rounded-full bg-foreground/15" />
              </span>
              <span className="min-w-0 flex-1 truncate rounded-(--radius-chip) bg-background px-2 py-0.5 text-center font-mono text-3xs text-muted-foreground">
                {token ? chatPageLink(token) : "Its link, once it's shared"}
              </span>
            </div>
          )}
          <div className="relative min-h-0 flex-1">
            {connection ? (
              <ChatPageView
                // Another version or person starts its chat afresh.
                key={`${appName}:${userId}`}
                connection={connection}
                agent={agent}
                settings={settings}
                theme={theme}
                onThemeChange={pick}
                desktopLink={
                  settings.desktop_link && token
                    ? chatPageDesktopLink(token)
                    : undefined
                }
                className="absolute inset-0"
              />
            ) : (
              <p className="p-4 text-xs text-muted-foreground">
                {RUNTIME_URL
                  ? "Loading…"
                  : "Set VITE_API_URL to preview the chat through the runtime."}
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
