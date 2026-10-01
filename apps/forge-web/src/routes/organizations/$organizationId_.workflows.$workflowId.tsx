import { createFileRoute } from "@tanstack/react-router"

import { WorkflowBuilderPage } from "@/components/workflows/workflow-builder"

/**
 * One of an organization's workflows, in its builder. Not nested in the workspace's
 * page (`$organizationId_`), because it draws its own sidebar (the step library) in
 * place of the workspace's. Only the organization's members get in.
 */
export const Route = createFileRoute("/organizations/$organizationId_/workflows/$workflowId")({
  component: WorkflowRoute,
})

function WorkflowRoute() {
  const { organizationId, workflowId } = Route.useParams()
  return <WorkflowBuilderPage key={workflowId} organizationId={organizationId} workflowId={workflowId} />
}
