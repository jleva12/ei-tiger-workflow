import * as React from "react"

import { Icon } from "@/components/forge/icon"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import type { KnowledgeBaseKind } from "../lib/api"
import { KNOWLEDGE_BASE_KINDS } from "../lib/kinds"

/**
 * The menu a new knowledge base starts from: a RAG one, of documents, or a
 * graph one, of code repositories. `trigger` is the button that opens it.
 */
export function NewKnowledgeBaseMenu({
  trigger,
  children,
  onPick,
}: {
  trigger: React.ReactElement
  children: React.ReactNode
  onPick: (kind: KnowledgeBaseKind) => void
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={trigger}>{children}</DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80">
        <DropdownMenuGroup>
          <DropdownMenuLabel>A knowledge base of…</DropdownMenuLabel>
          {(["rag", "graph"] as const).map((kind) => {
            const info = KNOWLEDGE_BASE_KINDS[kind]
            return (
              <DropdownMenuItem
                key={kind}
                onClick={() => onPick(kind)}
                className="items-start"
              >
                <Icon icon={info.icon} className="mt-0.5" />
                <span className="flex min-w-0 flex-col">
                  <span className="text-foreground">{info.label}</span>
                  <span className="text-2xs text-muted-foreground">
                    {info.summary}
                  </span>
                </span>
              </DropdownMenuItem>
            )
          })}
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
