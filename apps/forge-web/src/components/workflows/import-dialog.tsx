import * as React from "react"

import { ImportDialog as KitImportDialog } from "@/components/builder/import-dialog"
import { parseWorkflow, WORKFLOW_FORMAT, type WorkflowDocument } from "@/lib/workflows/document"

const NOUNS = { doc: "workflow", steps: "steps" }

/**
 * Reads a workflow from pasted JSON or a file (the builder kit's
 * ImportDialog, for workflows): the caller decides what importing does
 * (replace the canvas, or add a workflow).
 */
export function ImportDialog({
  organizationId,
  ...props
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  organizationId: string
  title: string
  description: string
  /** The import button's label. */
  action: string
  onImport: (doc: WorkflowDocument, needsLayout: boolean) => void
}) {
  const parse = React.useCallback(
    (text: string) => parseWorkflow(text, { organizationId }),
    [organizationId]
  )
  return <KitImportDialog {...props} format={WORKFLOW_FORMAT} nouns={NOUNS} parse={parse} />
}
