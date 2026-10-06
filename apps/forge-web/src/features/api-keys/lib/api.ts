import { useMutation, useQueryClient } from "@tanstack/react-query"

import { createNestedResource } from "@/lib/api/resource"
import { api } from "@/lib/api-instance"
import type { Audited } from "@/lib/hierarchy"

/**
 * An organization's API key (`/organizations/{org}/api-keys`): what an
 * outside app sends instead of a person's sign-in, holding one of the
 * organization's roles. Its secret is answered once, when it's made.
 */
export type ApiKeyRecord = Audited & {
  id: string
  organization_id: string
  name: string
  /** Its role there; null once the role has been deleted. */
  role: string | null
  role_name: string | null
  /** `fk_…` and its last four characters. */
  hint: string
  /** `apikey:<id>`: how runs and usage name its calls. */
  subject: string
  expires_at: string | null
  expired: boolean
  last_used_at: string | null
  created_by_name: string
}

/** A key just made: the key itself, this once. */
export type ApiKeyCreated = ApiKeyRecord & { secret: string }

export type ApiKeyCreate = {
  name: string
  role: string
  /** ISO time; null for never. */
  expires_at: string | null
}

export type ApiKeyUpdate = { name?: string; role?: string }

/** The role a key gets unless another is picked: API caller. */
export const DEFAULT_KEY_ROLE = "org:api"

export const organizationApiKeys = createNestedResource<
  ApiKeyRecord,
  { organizationId: string },
  { create: ApiKeyCreate; update: ApiKeyUpdate }
>({
  api,
  key: "organization-api-keys",
  path: ({ organizationId }) =>
    `/organizations/${encodeURIComponent(organizationId)}/api-keys`,
  label: "API key",
  updateMethod: "patch",
})

/** The organization's API keys, newest first. */
export function useOrganizationApiKeys(organizationId: string) {
  return organizationApiKeys.scope({ organizationId }).useList()
}

/**
 * Makes a key. Its answer carries the secret, so it's handed back to the
 * caller and never cached: only the list is refreshed.
 */
export function useCreateApiKey(organizationId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (input: ApiKeyCreate) =>
      api.post<ApiKeyCreated, ApiKeyCreate>(
        `/organizations/${encodeURIComponent(organizationId)}/api-keys`,
        input
      ),
    meta: { silent: true },
    onSuccess: () =>
      client.invalidateQueries({ queryKey: organizationApiKeys.keys.all }),
  })
}

/** How long a new key lasts, as the dialog offers it. */
export const EXPIRIES = [
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
  { value: "365", label: "1 year" },
  { value: "never", label: "No expiry" },
] as const

export type Expiry = (typeof EXPIRIES)[number]["value"]

/** When a key made now with that expiry stops working, as ISO; null for never. */
export function expiryOf(choice: Expiry, now = new Date()): string | null {
  if (choice === "never") return null
  return new Date(now.getTime() + Number(choice) * 86_400_000).toISOString()
}
