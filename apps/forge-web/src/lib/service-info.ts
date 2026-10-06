import { useQuery } from "@tanstack/react-query"

import { api } from "@/lib/api-instance"

/** What the admin API says about itself (`GET /info`). */
export type ServiceInfo = {
  name: string
  version: string
  /**
   * Whether anyone may call the runtime (agents and workflows) without an
   * API key or a sign-in.
   */
  agent_runtime_public: boolean
}

/** The admin API's name, version and runtime access: read once. */
export function useServiceInfo() {
  return useQuery({
    queryKey: ["admin-api", "info"],
    queryFn: ({ signal }) => api.get<ServiceInfo>("/info", { signal }),
    staleTime: Infinity,
    meta: { silent: true },
  })
}
