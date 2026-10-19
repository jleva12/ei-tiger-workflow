import * as React from "react"

import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { ApiError } from "@/lib/api/index"

import { SIGNED_IN, useMyAgents, type MyAgent } from "./session"

/** From how many agents the list offers a search. */
const SEARCH_FROM = 7

/**
 * Adds agents to the desktop app: the published agents of the organizations
 * the person signed in is a member of, by organization; each added runs as
 * them. Several can be added in one go.
 */
export function AddAgentDialog({
  open,
  onOpenChange,
  added,
  onAdd,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Whether an agent is in the app already. */
  added: (agent: MyAgent) => boolean
  onAdd: (agent: MyAgent) => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        {open && <AgentPicker added={added} onAdd={onAdd} />}
      </DialogContent>
    </Dialog>
  )
}

function AgentPicker({
  added,
  onAdd,
}: {
  added: (agent: MyAgent) => boolean
  onAdd: (agent: MyAgent) => void
}) {
  const agents = useMyAgents()
  const [search, setSearch] = React.useState("")
  const all = agents.data ?? []
  const words = search.trim().toLowerCase()
  const shown = words
    ? all.filter((agent) =>
        `${agent.name} ${agent.description} ${agent.organization_name}`
          .toLowerCase()
          .includes(words)
      )
    : all
  const groups = groupBy(shown, (agent) => agent.organization_name)

  return (
    <div className="grid gap-5">
      <DialogHeader>
        <DialogTitle>Add agents</DialogTitle>
        <DialogDescription>
          Your organizations' published agents. Each one you add runs as you,
          and keeps your conversations with it.
        </DialogDescription>
      </DialogHeader>
      {all.length >= SEARCH_FROM && (
        <Input
          autoFocus
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Search agents"
          aria-label="Search agents"
        />
      )}
      <div className="-mx-2 max-h-[min(60vh,28rem)] overflow-y-auto px-2">
        {!SIGNED_IN ? (
          <ErrorCallout title="No one is signed in">
            Single sign-on isn't set up yet. A development build signs in with
            VITE_DESKTOP_API_TOKEN (the desktop's dev server takes the
            console's, from make web-token).
          </ErrorCallout>
        ) : agents.error ? (
          <ErrorCallout
            title="Couldn't list your agents"
            action={
              <Button
                variant="outline"
                size="sm"
                onClick={() => void agents.refetch()}
              >
                Retry
              </Button>
            }
          >
            {agents.error instanceof ApiError && agents.error.status === 401
              ? "Your sign-in wasn't accepted: it may have expired."
              : agents.error.message}
          </ErrorCallout>
        ) : agents.isPending ? (
          <div className="flex flex-col gap-3 py-1" aria-busy="true">
            {[0, 1, 2].map((row) => (
              <div key={row} className="flex flex-col gap-1.5">
                <Skeleton className="h-4 w-40" />
                <Skeleton className="h-3 w-72 max-w-full" />
              </div>
            ))}
          </div>
        ) : all.length === 0 ? (
          <p className="py-2 text-sm text-muted-foreground">
            Your organizations have no published agents yet.
          </p>
        ) : shown.length === 0 ? (
          <p className="py-2 text-sm text-muted-foreground">
            No agents match "{search.trim()}".
          </p>
        ) : (
          <div className="flex flex-col gap-4">
            {groups.map(([organization, members]) => (
              <section key={organization} aria-label={organization}>
                <h3 className="px-2 pb-1 text-xs font-medium text-muted-foreground">
                  {organization}
                </h3>
                <ul className="flex flex-col">
                  {members.map((agent) => (
                    <AgentRow
                      key={agent.id}
                      agent={agent}
                      added={added(agent)}
                      onAdd={() => onAdd(agent)}
                    />
                  ))}
                </ul>
              </section>
            ))}
          </div>
        )}
      </div>
      <DialogFooter>
        <DialogClose render={<Button />}>Done</DialogClose>
      </DialogFooter>
    </div>
  )
}

function AgentRow({
  agent,
  added,
  onAdd,
}: {
  agent: MyAgent
  added: boolean
  onAdd: () => void
}) {
  return (
    <li className="flex items-center gap-3 rounded-(--radius-control) px-2 py-2 hover:bg-accent">
      <Icon
        icon="sparkles"
        size={16}
        className="shrink-0 text-muted-foreground"
      />
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="truncate text-sm font-medium text-foreground">
          {agent.name}
        </span>
        {agent.description && (
          <span className="truncate text-xs text-muted-foreground">
            {agent.description}
          </span>
        )}
      </div>
      {added ? (
        <span className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground">
          <Icon icon="check" size={14} />
          Added
        </span>
      ) : (
        <Button
          variant="outline"
          size="xs"
          className="shrink-0"
          onClick={onAdd}
          aria-label={`Add ${agent.name}`}
        >
          Add
        </Button>
      )}
    </li>
  )
}

/** Items in groups by a key, in the order the keys first come. */
function groupBy<T>(items: T[], key: (item: T) => string): [string, T[]][] {
  const groups = new Map<string, T[]>()
  for (const item of items) {
    const name = key(item)
    groups.set(name, [...(groups.get(name) ?? []), item])
  }
  return [...groups]
}
