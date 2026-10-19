import { useQuery } from "@tanstack/react-query"

import { api } from "@/lib/api-instance"

/** What the admin API says about itself (`GET /info`). */
export type ServiceInfo = {
  name: string
  version: string
  /**
   * The most seconds an agent waits for a workflow it called: a Workflow
   * tool's "Wait up to" at most. An older API leaves it out.
   */
  agent_workflow_wait_max?: number
}

/** The admin API's name, version and workflow wait cap: read once. */
export function useServiceInfo() {
  return useQuery({
    queryKey: ["admin-api", "info"],
    queryFn: ({ signal }) => api.get<ServiceInfo>("/info", { signal }),
    staleTime: Infinity,
    meta: { silent: true },
  })
}
