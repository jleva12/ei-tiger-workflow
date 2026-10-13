import * as React from "react"

import { ErrorCallout } from "@/components/forge/feedback"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { useNodeCatalogs } from "@/features/adk-workflows/lib/api"

/**
 * Shows its children once the node catalogs are loaded (and installed):
 * what every kind of node is called, its ways out and what a new one
 * starts with, a workflow's and the Agents canvas's. Wrap anything that
 * reads a kind: the builders, their lists, a run's page.
 */
export function NodeCatalogGate({ children }: { children: React.ReactNode }) {
  const catalogs = useNodeCatalogs()
  if (catalogs.error)
    return (
      <ErrorCallout
        title="Couldn't load the kinds of node"
        action={
          <Button variant="outline" size="sm" onClick={catalogs.retry}>
            Retry
          </Button>
        }
      >
        {catalogs.error.message}
      </ErrorCallout>
    )
  if (!catalogs.ready)
    return (
      <div className="flex flex-col gap-3" aria-busy="true">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-[60vh] w-full rounded-(--radius-card)" />
      </div>
    )
  return children
}
