import { Wrench01Icon } from "@hugeicons/core-free-icons"

import { composerChip } from "@/components/forge/assistant"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import type { AgentTool } from "./lib/agent-tools"
import { cn } from "cn"

/**
 * In the composer, beside the model section: which of the agent's tools it
 * may use. The choice goes with the next message (`useAgent`'s `runState`).
 */
export function ToolsMenu({
  tools,
  enabledNames,
  setToolEnabled,
}: {
  tools: AgentTool[]
  enabledNames: string[] | undefined
  setToolEnabled: (name: string, on: boolean) => void
}) {
  if (tools.length === 0) return null
  const enabled = new Set(enabledNames)
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button
            variant="ghost"
            size="xs"
            className={cn(composerChip, "shrink-0")}
          />
        }
        aria-label={`Tools: ${enabled.size} of ${tools.length} on`}
      >
        <Icon icon={Wrench01Icon} data-icon="inline-start" />
        <span className="@max-[42rem]:hidden">Tools</span>
        <span className="text-foreground tabular-nums">
          {enabled.size}/{tools.length}
        </span>
        <Icon
          icon="down"
          data-icon="inline-end"
          className="@max-[36rem]:hidden"
        />
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="start" className="w-72">
        <DropdownMenuGroup>
          <DropdownMenuLabel>The agent may use</DropdownMenuLabel>
          {tools.map((tool) => (
            <DropdownMenuCheckboxItem
              key={tool.name}
              checked={enabled.has(tool.name)}
              onCheckedChange={(on) => setToolEnabled(tool.name, on)}
            >
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="font-mono">{tool.name}</span>
                {tool.description && (
                  <span className="text-2xs text-muted-foreground">
                    {tool.description}
                  </span>
                )}
              </span>
            </DropdownMenuCheckboxItem>
          ))}
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
