import * as React from "react"
import { useQueryClient } from "@tanstack/react-query"

import { useBuilderAutosave, type Autosave } from "@/components/builder/autosave"
import {
  cacheWorkflow,
  organizationWorkflows,
  saveWorkflow,
  type WorkflowRecord,
} from "@/lib/workflows/api"
import type { WorkflowDocument } from "@/lib/workflows/document"

export type { Autosave, SaveState } from "@/components/builder/autosave"

/**
 * Saving the workflow to the organization as it's built: the builder kit's
 * autosave (components/builder/autosave), through the workflows API.
 */
export function useAutosave(organizationId: string, initial: WorkflowRecord): Autosave {
  const client = useQueryClient()
  const io = React.useMemo(
    () => ({
      save: (id: string, body: { document: WorkflowDocument; revision: number }, { keepalive }: { keepalive: boolean }) =>
        saveWorkflow(organizationId, id, body, { keepalive }),
      fetch: (id: string) => organizationWorkflows.scope({ organizationId }).requests.detail(id),
      cache: (record: WorkflowRecord) => cacheWorkflow(client, record),
    }),
    [organizationId, client]
  )
  return useBuilderAutosave(initial, io)
}
