import * as React from "react"

import { PageEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Skeleton } from "@/components/ui/skeleton"
import type { ApiError } from "@/lib/api"
import type { Organization, NodeInput, PermissionKey } from "@/lib/hierarchy"
import { useActorLabel } from "@/lib/users"
import { parseTimestamp } from "@/lib/timestamps"
import type { Level } from "./levels"
import { DeleteNodeDialog, NodeDialog } from "./node-dialogs"

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" })

/** A node's description and when, and by whom, it was created and changed. */
export function NodeSummary({ node }: { node: Organization | undefined }) {
  const actor = useActorLabel()
  if (!node) {
    return (
      <div className="mb-5 flex flex-col gap-2">
        <Skeleton className="h-4 w-72 max-w-full" />
        <Skeleton className="h-3 w-96 max-w-full" />
      </div>
    )
  }
  return (
    <div className="mb-5 flex flex-col gap-1.5">
      <p className="text-sm text-muted-foreground">
        {node.description || "No description."}
      </p>
      <p className="text-2xs text-subtle">
        Created {dateFormat.format(parseTimestamp(node.created_at))} by{" "}
        {actor(node.created_by)} · Updated{" "}
        {dateFormat.format(parseTimestamp(node.updated_at))} by{" "}
        {actor(node.updated_by)}
      </p>
    </div>
  )
}

type NodeActionsProps = {
  level: Level
  node: Organization | undefined
  /** What the user may do in the node's own scope. */
  can: (permission: PermissionKey) => boolean
  update: (input: NodeInput) => Promise<unknown>
  remove: () => Promise<unknown>
  /** After a delete, e.g. go to the parent. */
  onDeleted: () => void
}

/** Edit and delete for the page's own node, as a header menu with dialogs. */
export function NodeActions({
  level,
  node,
  can,
  update,
  remove,
  onDeleted,
}: NodeActionsProps) {
  const [editing, setEditing] = React.useState(false)
  const [deleting, setDeleting] = React.useState<Organization>()
  const canUpdate = can(`${level.resource}:update`)
  const canDelete = can(`${level.resource}:delete`)
  if (!node || !(canUpdate || canDelete)) return null

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger
          render={
            <Button
              variant="outline"
              size="icon"
              aria-label={`${level.title} actions`}
            />
          }
        >
          <Icon icon="more" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="min-w-44">
          {canUpdate && (
            <DropdownMenuItem onClick={() => setEditing(true)}>
              <Icon icon="settings" />
              Edit {level.noun}…
            </DropdownMenuItem>
          )}
          {canDelete && (
            <>
              {canUpdate && <DropdownMenuSeparator />}
              <DropdownMenuItem
                variant="destructive"
                onClick={() => setDeleting(node)}
              >
                <Icon icon="close" />
                Delete {level.noun}…
              </DropdownMenuItem>
            </>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
      <NodeDialog
        level={level}
        open={editing}
        onOpenChange={setEditing}
        node={node}
        onSubmit={update}
      />
      <DeleteNodeDialog
        level={level}
        node={deleting}
        onClose={() => setDeleting(undefined)}
        onConfirm={async () => {
          await remove()
          onDeleted()
        }}
      />
    </>
  )
}

type NodeUnavailableProps = {
  level: Level
  error: ApiError
  onRetry: () => void
  /** Where to go instead, e.g. the parent. */
  back: { label: string; onClick: () => void }
}

/** Why a node's page can't show it: gone, not allowed, or a failed load. */
export function NodeUnavailable({
  level,
  error,
  onRetry,
  back,
}: NodeUnavailableProps) {
  const backButton = (
    <Button variant="outline" size="sm" onClick={back.onClick}>
      Back to {back.label}
    </Button>
  )
  // 422: the ID in the URL isn't one the API accepts.
  if (error.status === 404 || error.status === 422) {
    return (
      <PageEmpty
        illustration="search"
        title={`${level.title} not found`}
        description="It may have been deleted, or the link is wrong."
      >
        {backButton}
      </PageEmpty>
    )
  }
  if (error.status === 403) {
    return (
      <PageEmpty
        illustration="locked"
        title={`You can't view this ${level.noun}`}
        description="None of your roles covers it. Ask an administrator for access."
      >
        {backButton}
      </PageEmpty>
    )
  }
  return (
    <ErrorCallout
      title={`Couldn't load the ${level.noun}`}
      action={
        <Button variant="outline" size="sm" onClick={onRetry}>
          Retry
        </Button>
      }
    >
      {error.message}
    </ErrorCallout>
  )
}
