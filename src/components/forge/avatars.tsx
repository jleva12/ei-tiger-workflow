import * as React from "react"
import { mergeProps } from "@base-ui/react/merge-props"
import { useRender } from "@base-ui/react/use-render"
import { cn } from "cn"

import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import { Icon } from "./icon"

const agentOrbs = [
  "orb-agent-0",
  "orb-agent-1",
  "orb-agent-2",
  "orb-agent-3",
  "orb-agent-4",
] as const

type Agent = {
  id: string
  name: string
  role?: string
}

/**
 * The workspace mark: a 22px rounded square with the reference's gradient.
 * Pass `render={<button />}` to make it interactive (e.g. a profile switch).
 */
function WorkspaceOrb({
  size = "default",
  className,
  render,
  ...props
}: useRender.ComponentProps<"span"> & { size?: "default" | "lg" }) {
  return useRender({
    defaultTagName: "span",
    props: mergeProps<"span">(
      {
        className: cn(
          "inline-block size-[22px] shrink-0 rounded-(--radius-soft) bg-orb-workspace",
          size === "lg" && "size-[26px]",
          className
        ),
      },
      props
    ),
    render,
    state: { slot: "workspace-orb" },
  })
}

/** A single gradient agent avatar. `variant` picks one of five gradients. */
function AgentOrb({
  variant = 0,
  size = "default",
  className,
  ...props
}: React.ComponentProps<"span"> & {
  variant?: number
  size?: "default" | "lg"
}) {
  return (
    <span
      data-slot="agent-orb"
      className={cn(
        "inline-block size-[17px] shrink-0 rounded-full border-[1.5px] border-background",
        size === "lg" && "size-[22px]",
        agentOrbs[Math.abs(variant) % agentOrbs.length],
        className
      )}
      {...props}
    />
  )
}

/**
 * Overlapping agent orbs with name tooltips, capped at `max` with a `+N`
 * overflow. Renders a quiet "No agents" marker when the list is empty.
 */
function AgentAvatars({
  agents,
  max = 5,
  size = "default",
  emptyLabel = "No agents",
  hideEmptyLabel = false,
  className,
}: {
  agents: Agent[]
  max?: number
  size?: "default" | "lg"
  emptyLabel?: string
  /** Show only the robot glyph when empty (dense tables). */
  hideEmptyLabel?: boolean
  className?: string
}) {
  if (!agents.length)
    return (
      <span
        data-slot="agent-avatars"
        className={cn(
          "flex items-center justify-end gap-[5px] text-3xs whitespace-nowrap text-subtle",
          className
        )}
      >
        <Icon icon="robot" size={15} />
        <span className={cn(hideEmptyLabel && "sr-only")}>{emptyLabel}</span>
      </span>
    )
  return (
    <div
      data-slot="agent-avatars"
      role="group"
      aria-label={agents.map((agent) => agent.name).join(", ")}
      className={cn("flex items-center justify-end", className)}
    >
      {agents.slice(0, max).map((agent, index) => (
        <Tooltip key={agent.id}>
          <TooltipTrigger
            render={
              <AgentOrb
                tabIndex={0}
                aria-label={agent.name}
                variant={index}
                size={size}
                className={cn(
                  "-ml-1.5 first:ml-0",
                  size === "lg" && "-ml-[5px]"
                )}
              />
            }
          />
          <TooltipContent>
            {agent.name}
            {agent.role && ` · ${agent.role}`}
          </TooltipContent>
        </Tooltip>
      ))}
      {agents.length > max && (
        <span className="ml-[3px] text-4xs text-subtle">
          +{agents.length - max}
        </span>
      )}
    </div>
  )
}

export { WorkspaceOrb, AgentOrb, AgentAvatars, type Agent }
