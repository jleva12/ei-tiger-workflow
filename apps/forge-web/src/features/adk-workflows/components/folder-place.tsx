import * as React from "react"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { useAgentBuilder } from "@/features/adk-workflows/components/agent-store"
import {
  folderChoices,
  placeLabel,
  placeOf,
  type FolderChoice,
} from "@/features/adk-workflows/lib/folders"

/**
 * Where a node that works in a folder works, under its folder setting: the
 * path from the run's folder, as far as the builder can tell before a run
 * (a part filled in as it runs is shown as written), and the step whose
 * folder it is. With `onChange`, the folders earlier steps work in (a
 * clone's, a Pi agent's) are offered, a click each, as the run's folder is.
 */
export function FolderPlace({
  id,
  value,
  verb,
  onChange,
}: {
  /** The node. */
  id: string
  /** Its folder setting. */
  value: string
  /** What it does there: "Clones into", "Works in". */
  verb: string
  /** Set the folder; without it, nothing is offered. */
  onChange?: (value: string) => void
}) {
  const nodes = useAgentBuilder((s) => s.nodes)
  const scopeOf = useAgentBuilder((s) => s.scopeOf)
  const readOnly = useAgentBuilder((s) => s.readOnly)
  const steps = React.useMemo(
    () => new Map(nodes.map((node) => [node.id, node.data])),
    [nodes]
  )
  const place = placeOf(value, steps.get(id), steps)
  // The earlier steps' folders, each place once: a clone's, not the branch's after it too.
  const choices = React.useMemo(() => {
    if (!onChange) return []
    const earlier = scopeOf(id).roots.get("steps")?.type
    const ids =
      earlier?.kind === "object" ? Object.keys(earlier.properties) : []
    const seen = new Set<string>()
    return folderChoices(ids, steps).filter((choice) => {
      const label = placeLabel(choice.place)
      if (seen.has(label) || !choice.place.parts.length) return false
      seen.add(label)
      return true
    })
  }, [onChange, scopeOf, id, steps])
  const written = value.trim()

  return (
    <div className="flex flex-col gap-1.5 rounded-(--radius-item) bg-muted px-2.5 py-2 text-xs">
      <div className="flex min-w-0 flex-wrap items-center gap-x-1 gap-y-0.5">
        <span className="text-muted-foreground">{verb}</span>
        <Tooltip>
          <TooltipTrigger
            delay={300}
            render={
              <span className="inline-flex items-center gap-1 font-medium text-foreground underline decoration-dotted underline-offset-2" />
            }
          >
            <Icon icon="folder" className="size-3.5" />
            Run folder
          </TooltipTrigger>
          <TooltipContent className="max-w-72">
            A folder of the run&apos;s own on the worker:{" "}
            <span className="font-mono">
              &lt;workspaces&gt;/&lt;organization&gt;/&lt;workflow&gt;/&lt;run&gt;
            </span>
            . Each attempt of the run works in the same one, and it&apos;s kept
            after the run ends. Steps read its path as{" "}
            <span className="font-mono">{"{{ state.workspace }}"}</span>.
          </TooltipContent>
        </Tooltip>
        {place.parts.map((part, index) => (
          <React.Fragment key={index}>
            <span className="text-subtle">/</span>
            <span
              className={cn(
                "font-mono break-all",
                part.later ? "text-muted-foreground italic" : "text-foreground"
              )}
            >
              {part.text}
            </span>
          </React.Fragment>
        ))}
      </div>
      {place.from && (
        <span className="text-muted-foreground">
          The folder {place.from.name} works in.
        </span>
      )}
      {place.outside && (
        <span className="text-destructive">
          That leads outside the run&apos;s folder: the step would fail.
        </span>
      )}
      {onChange && !readOnly && choices.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-muted-foreground">Use</span>
          <Pick active={!written} onClick={() => onChange("")}>
            Run folder
          </Pick>
          {choices.map((choice) => (
            <Pick
              key={choice.id}
              active={written === choice.value}
              onClick={() => onChange(choice.value)}
              title={`${choice.name}'s folder: ${placeLabel(choice.place)}`}
            >
              {choiceLabel(choice)}
            </Pick>
          ))}
        </div>
      )}
    </div>
  )
}

const choiceLabel = (choice: FolderChoice) =>
  `${choice.name} · ${placeLabel(choice.place)}`

function Pick({
  active,
  onClick,
  title,
  children,
}: {
  active: boolean
  onClick: () => void
  title?: string
  children: React.ReactNode
}) {
  return (
    <Button
      type="button"
      size="xs"
      variant="outline"
      aria-pressed={active}
      title={title}
      onClick={onClick}
      className={cn("max-w-full", active && "font-medium text-foreground")}
    >
      {active && <Icon icon="check" data-icon="inline-start" />}
      <span className="truncate">{children}</span>
    </Button>
  )
}
