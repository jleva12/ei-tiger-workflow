import * as React from "react"
import { useAuiState } from "@assistant-ui/react"
import { AlertCircleIcon } from "@hugeicons/core-free-icons"

import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverHeader,
  PopoverTitle,
  PopoverTrigger,
} from "@/components/ui/popover"
import { Skeleton } from "@/components/ui/skeleton"
import {
  useAssistantCapabilities,
  type AssistantToolset,
} from "@/lib/assistant-capabilities"
import { FORGE_AGENT_SERVER } from "@/lib/forge-agent"
import type { PageContext } from "@/lib/page-context"

/** Where the assistant's tools come from: the page, or nowhere. */
function onThisPage(
  page: PageContext | null,
  screens: { title: string }[] | undefined
) {
  if (!page) return "It isn't seeing your page: sharing it is off."
  if (!screens?.length) return "Nothing on this page adds tools."
  return `On this page: ${screens.map((screen) => screen.title).join(" · ")}.`
}

function ToolsetEntry({ toolset }: { toolset: AssistantToolset }) {
  return (
    <li className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2">
        <h3 className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">
          {toolset.title}
        </h3>
        {toolset.fromEarlier && (
          <Badge variant="secondary">From earlier in this conversation</Badge>
        )}
      </div>
      <p className="text-muted-foreground">{toolset.description}</p>
      {toolset.tools && toolset.tools.length > 0 && (
        <ul className="flex flex-col gap-1.5 border-l border-border pl-2.5">
          {toolset.tools.map((tool) => (
            <li key={tool.name} className="flex flex-col gap-0.5">
              <span className="flex items-center gap-1.5">
                <code className="min-w-0 truncate font-mono text-2xs text-foreground">
                  {tool.name}
                </code>
                {tool.asksFirst && (
                  <Badge variant="outline">Asks you first</Badge>
                )}
              </span>
              {tool.description && (
                <span className="text-muted-foreground">
                  {tool.description}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

function CapabilitiesBody({
  userId,
  page,
  sessionId,
}: {
  userId: string
  page: PageContext | null
  sessionId: string | undefined
}) {
  const capabilities = useAssistantCapabilities({
    userId,
    page,
    sessionId,
    tools: true,
  })
  if (capabilities.isPending) {
    return (
      <div className="flex flex-col gap-2" aria-busy="true">
        <Skeleton className="h-4 w-40" />
        <Skeleton className="h-3 w-64" />
        <Skeleton className="h-3 w-56" />
      </div>
    )
  }
  if (capabilities.isError) {
    return (
      <ErrorCallout
        title="Couldn't read what it has here"
        action={
          <Button
            variant="outline"
            size="sm"
            onClick={() => void capabilities.refetch()}
          >
            Retry
          </Button>
        }
      >
        {capabilities.error.message}
      </ErrorCallout>
    )
  }
  const { screens, toolsets } = capabilities.data
  return (
    <>
      <PopoverDescription>{onThisPage(page, screens)}</PopoverDescription>
      {toolsets.length ? (
        <ul className="flex flex-col gap-4">
          {toolsets.map((toolset) => (
            <ToolsetEntry key={toolset.name} toolset={toolset} />
          ))}
        </ul>
      ) : (
        <p className="text-muted-foreground">
          No tools here. It can still answer questions about Forge.
        </p>
      )}
    </>
  )
}

/**
 * The assistant's "!" button: what it can help with where the person is,
 * the toolsets and tools its screen configuration gives it on this page
 * (and those its conversation kept from earlier pages), each acting as
 * them. Render it inside the assistant's provider, with the page it sees
 * (null while they don't share it).
 */
export function AssistantCapabilitiesButton({
  userId,
  page,
}: {
  userId: string
  page: PageContext | null
}) {
  const [open, setOpen] = React.useState(false)
  const sessionId = useAuiState((s) => s.threadListItem.remoteId)
  if (!FORGE_AGENT_SERVER) return null
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <TooltipIconButton
            tooltip="What it can help with here"
            side="bottom"
            className="size-7 rounded-md p-0 text-muted-foreground hover:text-foreground"
          />
        }
      >
        <Icon icon={AlertCircleIcon} size={15} />
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="max-h-[min(32rem,70vh)] w-96 max-w-[calc(100vw-2rem)] overflow-y-auto"
        tabIndex={0}
      >
        <PopoverHeader>
          <PopoverTitle>What it can help with here</PopoverTitle>
        </PopoverHeader>
        {open && (
          <CapabilitiesBody userId={userId} page={page} sessionId={sessionId} />
        )}
        <p className="border-t border-border pt-2.5 text-muted-foreground">
          It acts as you: it only sees and changes what you can in Forge.
        </p>
      </PopoverContent>
    </Popover>
  )
}
