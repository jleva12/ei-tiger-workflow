import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { PrimaryAction } from "@/components/forge/app-shell"
import { Icon } from "@/components/forge/icon"
import { registryConfig, registryEnv, registryRepo } from "./registry"
import { Caption, CodeBlock } from "./specimen"

const items = [
  ["theme", "Tokens, fonts, focus ring, scrollbars, orb utilities"],
  ["app-shell", "AppShell, Topbar, ViewToolbar, PageContent, footer"],
  ["icon-rail", "IconRail, AppMark, RailButton"],
  ["workspace-sidebar", "Sidebar brand, NavItem, ProjectItem, status"],
  ["toolbar", "View tabs, LayoutSwitch, SearchField, ToolbarButton"],
  ["task-list", "Status bands and the grouped task table"],
  ["data-table", "Spreadsheet-grade TanStack Table v9 grid"],
  ["kanban", "Board, columns and cards"],
  ["empty-state", "Workspace, panel and page empty states"],
  ["feedback", "Error callouts, preview banner, load more"],
  ["activity", "Event list, stat grid, view headings"],
  ["task-sheet", "Create/inspect sheets and compact forms"],
  ["task-page", "Detail header, line tabs, submission layout"],
  ["run-log", "Job list and the dark log console"],
  ["changes", "Changed files and GitHub-style diffs"],
  ["handoff", "Verification rounds, handoffs, findings"],
  ["metrics-table", "Grouped numeric tables"],
] as const

export function InstallDialog() {
  return (
    <Dialog>
      <DialogTrigger render={<PrimaryAction icon="download" />}>
        Install
      </DialogTrigger>
      <DialogContent className="max-h-[calc(100dvh-32px)] w-[min(640px,calc(100%-32px))] gap-5 overflow-y-auto sm:max-w-none">
        <DialogHeader className="gap-2 pr-6">
          <DialogTitle className="text-xl">
            Use the Forge UI design system
          </DialogTitle>
          <DialogDescription className="text-sm/[1.6]">
            This project is a shadcn registry, served from{" "}
            <code>packages/forge-ui</code> in the private{" "}
            <code>{registryRepo}</code> repo on GitHub. Any project can install
            from it with a GitHub token that can read the repo.
          </DialogDescription>
        </DialogHeader>
        <div className="flex min-w-0 flex-col gap-4">
          <div>
            <Caption>1 · Initialise with the same preset</Caption>
            <CodeBlock code="npx shadcn@latest init --preset b27GdBA3 --base base" />
          </div>
          <div>
            <Caption>2 · Add a GitHub token to .env.local</Caption>
            <CodeBlock code={registryEnv} />
          </div>
          <div>
            <Caption>3 · Add the registry to components.json</Caption>
            <CodeBlock code={registryConfig} />
          </div>
          <div>
            <Caption>4 · Install</Caption>
            <CodeBlock
              code={`npx shadcn@latest add @forge-ui/theme @forge-ui/all`}
            />
          </div>
          <div>
            <Caption>Items</Caption>
            <ul className="divide-y rounded-(--radius-card) border text-xs">
              {items.map(([name, text]) => (
                <li key={name} className="flex items-baseline gap-3 px-3 py-2">
                  <code className="w-36 shrink-0 font-mono text-foreground">
                    @forge-ui/{name}
                  </code>
                  <span className="text-muted-foreground">{text}</span>
                </li>
              ))}
            </ul>
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="self-start text-muted-foreground"
            nativeButton={false}
            render={
              <a href="/r/registry.json" target="_blank" rel="noreferrer" />
            }
          >
            <Icon icon="external" data-icon="inline-start" />
            View registry index
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
