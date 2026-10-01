import * as React from "react"
import { Redo02Icon, Undo02Icon } from "@hugeicons/core-free-icons"
import { useReactFlow } from "@xyflow/react"
import { cn } from "cn"

import { ToolbarFilters, ViewToolbar } from "@/components/forge/app-shell"
import { Icon } from "@/components/forge/icon"
import type { IconProp } from "@/components/forge/icons"
import {
  ToolbarButton,
  ViewTabsList,
  ViewTabsTrigger,
} from "@/components/forge/toolbar"
import { Button } from "@/components/ui/button"
import { Kbd } from "@/components/ui/kbd"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { Spinner } from "@/components/ui/spinner"
import { Tabs } from "@/components/ui/tabs"
import { toast } from "@/components/ui/toast"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { toApiError } from "@/lib/api"
import { issueSummary } from "@/lib/builder/types"
import type { Autosave } from "./autosave"
import { IssueList } from "./issue-list"
import {
  useBuilder,
  useBuilderApi,
  type BuilderView,
  type FlowEdge,
  type FlowNode,
} from "./store"
import { useBuilderUi } from "./ui"
import { measureFlow, useOpenStep } from "./utils"

/*
 * A builder's top bar and toolbar: where saving stands, what's left to
 * fix, the Canvas / JSON views, undo and redo, and Tidy up.
 */

export function BuilderToolbar({
  details,
  onDetails,
}: {
  details: boolean
  onDetails: () => void
}) {
  const api = useBuilderApi()
  const flow = useReactFlow<FlowNode, FlowEdge>()
  const { docIcon } = useBuilderUi()
  const view = useBuilder((s) => s.view)
  const canUndo = useBuilder((s) => s.past.length > 0)
  const canRedo = useBuilder((s) => s.future.length > 0)
  const steps = useBuilder((s) => s.nodes.length)
  return (
    <ViewToolbar>
      <Tabs
        value={view}
        onValueChange={(value) =>
          api.getState().setView(String(value) as BuilderView)
        }
      >
        <ViewTabsList aria-label="Views">
          <ViewTabsTrigger value="canvas" icon={docIcon}>
            Canvas
          </ViewTabsTrigger>
          <ViewTabsTrigger value="json" icon="code">
            JSON
          </ViewTabsTrigger>
        </ViewTabsList>
      </Tabs>
      {view === "canvas" && (
        <ToolbarFilters>
          <div className="flex items-center">
            <HistoryButton
              label="Undo"
              keys={["⌘", "Z"]}
              icon={Undo02Icon}
              disabled={!canUndo}
              onClick={() => api.getState().undo()}
            />
            <HistoryButton
              label="Redo"
              keys={["⇧", "⌘", "Z"]}
              icon={Redo02Icon}
              disabled={!canRedo}
              onClick={() => api.getState().redo()}
            />
          </div>
          <ToolbarButton
            icon="layers"
            disabled={steps < 2}
            onClick={() => {
              const store = api.getState()
              store.arrange(measureFlow(flow))
              store.requestFit()
            }}
          >
            Tidy up
          </ToolbarButton>
          <ToolbarButton
            icon="settings"
            aria-pressed={details}
            onClick={onDetails}
            className="hidden @max-[900px]/shell:inline-flex"
          >
            Details
          </ToolbarButton>
        </ToolbarFilters>
      )}
    </ViewToolbar>
  )
}

function HistoryButton({
  label,
  keys,
  icon,
  disabled,
  onClick,
}: {
  label: string
  keys: string[]
  icon: IconProp
  disabled: boolean
  onClick: () => void
}) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={label}
            disabled={disabled}
            onClick={onClick}
            className="text-muted-foreground hover:text-foreground"
          />
        }
      >
        <Icon icon={icon} size={16} />
      </TooltipTrigger>
      <TooltipContent>
        {label}
        <span className="flex gap-0.5">
          {keys.map((key) => (
            <Kbd key={key}>{key}</Kbd>
          ))}
        </span>
      </TooltipContent>
    </Tooltip>
  )
}

const time = (at: number) =>
  new Date(at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })

/**
 * Where saving stands, in the top bar: quiet while it's saved, a spinner
 * while it saves, and what to do when it can't (retry, or choose between
 * a teammate's version and this one).
 */
export function SaveStatus({
  saving,
  organizationName,
}: {
  saving: Autosave
  organizationName: string
}) {
  const { nouns, permission } = useBuilderUi()
  const { state } = saving
  if (state.kind === "conflict") return <Conflict saving={saving} />
  const look =
    state.kind === "saved"
      ? { icon: "check" as const, text: "Saved", tone: "text-subtle" }
      : state.kind === "saving"
        ? { icon: undefined, text: "Saving…", tone: "text-subtle" }
        : state.kind === "retrying"
          ? {
              icon: "warning" as const,
              text: "Not saved yet",
              tone: "text-tone-amber-foreground",
            }
          : state.status === 403
            ? {
                icon: "info" as const,
                text: "View only",
                tone: "text-muted-foreground",
              }
            : {
                icon: "warning" as const,
                text: "Not saved",
                tone: "text-destructive",
              }
  const explanation =
    state.kind === "saved"
      ? `Saved to ${organizationName} at ${time(state.at)}. Everyone in the organization sees this version.`
      : state.kind === "saving"
        ? `Saving to ${organizationName}…`
        : state.kind === "retrying"
          ? `Couldn't reach Forge (${state.error}). Trying again at ${time(state.next)}; click to try now. Keep the page open, or export its JSON.`
          : state.status === 403
            ? `You can look at ${organizationName}'s ${nouns.docs} but not change them, so your changes aren't saved. Ask an organization admin for ${permission}.`
            : `Forge wouldn't save it: ${state.error} Your next change tries again.`
  const retryable =
    state.kind === "retrying" ||
    (state.kind === "refused" && state.status !== 403)
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          retryable ? (
            <button
              type="button"
              onClick={saving.saveNow}
              className={cn(
                "flex items-center gap-1.5 rounded-(--radius-control) px-1.5 text-xs hover:underline hover:underline-offset-3 @max-[800px]/shell:hidden",
                look.tone
              )}
            />
          ) : (
            <span
              tabIndex={0}
              aria-live="polite"
              className={cn(
                "flex items-center gap-1.5 rounded-(--radius-control) px-1.5 text-xs @max-[800px]/shell:hidden",
                look.tone
              )}
            />
          )
        }
      >
        {look.icon ? (
          <Icon icon={look.icon} size={14} />
        ) : (
          <Spinner className="size-3.5" />
        )}
        {look.text}
      </TooltipTrigger>
      <TooltipContent className="max-w-72">{explanation}</TooltipContent>
    </Tooltip>
  )
}

/** Someone saved it since: take their version, or save this one over it. */
function Conflict({ saving }: { saving: Autosave }) {
  const { nouns } = useBuilderUi()
  const [open, setOpen] = React.useState(true)
  const [busy, setBusy] = React.useState<"theirs" | "mine">()
  const run = async (choice: "theirs" | "mine") => {
    setBusy(choice)
    try {
      await (choice === "theirs" ? saving.loadTheirs() : saving.keepMine())
      setOpen(false)
    } catch (caught) {
      toast.add({
        title: "Couldn't read the organization's version",
        description: toApiError(caught).message,
        type: "error",
      })
    } finally {
      setBusy(undefined)
    }
  }
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <Button
            variant="ghost"
            size="sm"
            className="gap-1.5 font-normal text-destructive hover:text-destructive"
          />
        }
      >
        <Icon icon="warning" size={14} />
        <span className="@max-[600px]/shell:sr-only">Changed elsewhere</span>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 gap-3 p-3">
        <div className="flex flex-col gap-1">
          <p className="text-xs font-medium text-foreground">
            Someone else saved this {nouns.doc}
          </p>
          <p className="text-xs/[1.6] text-muted-foreground">
            A teammate saved it since your last save, so your latest changes
            aren&apos;t saved. Take their version (undo brings yours back), or
            save yours over theirs.
          </p>
        </div>
        <div className="flex justify-end gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={Boolean(busy)}
            onClick={() => void run("theirs")}
          >
            {busy === "theirs" && <Spinner data-icon="inline-start" />}
            Load their version
          </Button>
          <Button
            size="sm"
            disabled={Boolean(busy)}
            onClick={() => void run("mine")}
          >
            {busy === "mine" && <Spinner data-icon="inline-start" />}
            Save mine over it
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  )
}

/** How many issues stand in the way, opening the list of them. */
export function IssuesButton() {
  const { ready } = useBuilderUi()
  const issues = useBuilder((s) => s.issues)
  const openStep = useOpenStep()
  const [open, setOpen] = React.useState(false)
  const errors = issues.filter((i) => i.level === "error").length
  const summary = issueSummary(issues)
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <Button
            variant="ghost"
            size="sm"
            className={cn(
              "gap-1.5 font-normal",
              errors
                ? "text-destructive hover:text-destructive"
                : issues.length
                  ? "text-tone-amber-foreground hover:text-tone-amber-foreground"
                  : "text-muted-foreground"
            )}
          />
        }
      >
        <Icon
          icon={errors ? "failed" : issues.length ? "warning" : "completed"}
          size={14}
          className={!issues.length ? "text-signal-success" : undefined}
        />
        <span className="@max-[600px]/shell:sr-only">
          {issues.length ? `${issues.length} to fix` : "Ready"}
        </span>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 gap-2 p-2">
        <p className="px-1.5 pt-1 text-xs font-medium text-foreground">
          {summary ? `Before it can run: ${summary}` : "Nothing to fix"}
        </p>
        {issues.length ? (
          <div className="max-h-80 overflow-y-auto px-1.5">
            <IssueList
              issues={issues}
              onOpen={(step, field) => {
                setOpen(false)
                openStep(step, field)
              }}
            />
          </div>
        ) : (
          <p className="px-1.5 pb-1 text-xs text-muted-foreground">{ready}</p>
        )}
      </PopoverContent>
    </Popover>
  )
}
