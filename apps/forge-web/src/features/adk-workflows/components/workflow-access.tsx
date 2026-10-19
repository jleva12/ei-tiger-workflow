import { toast } from "@/components/ui/toast"
import { AccessSetting } from "@/features/agent-access/components/access-setting"
import {
  useWorkflowAccess,
  useWorkflowAccessSetting,
  type AgentRecord,
} from "@/features/adk-workflows/lib/api"
import { toApiError } from "@/lib/api/index"

/**
 * Who may run the workflow: some of its organization's agent permissions, or
 * anyone who may run its workflows (`AccessSetting`). It counts wherever a
 * run starts: here, the runtime, a chat agent's workflow tool, and another
 * workflow running it.
 */
export function WorkflowAccessSetting({
  record,
  canManage,
}: {
  record: AgentRecord
  canManage: boolean
}) {
  const access = useWorkflowAccess(record)
  const setting = useWorkflowAccessSetting(record.organization_id, record.id)
  return (
    <AccessSetting
      organizationId={record.organization_id}
      verb="run"
      chosen={access?.permission_ids ?? []}
      canManage={canManage}
      saving={setting.isPending}
      limitedNote="Workflows and chat agents that run it are limited too."
      onChange={(next) =>
        setting.mutate(next, {
          onError: (caught) =>
            toast.add({
              title: "Couldn't change who may run it",
              description: toApiError(caught).message,
              type: "error",
            }),
        })
      }
    />
  )
}
