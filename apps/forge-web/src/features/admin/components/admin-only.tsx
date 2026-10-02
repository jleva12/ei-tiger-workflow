import * as React from "react"

import { PageEmpty } from "@/components/forge/empty-state"
import { Guard } from "@/lib/user-access-provider"
import { SITE_ADMIN } from "@/app/workspace-scope"

/**
 * Site administration (organizations, users and access) is for
 * site administrators: anyone else gets a locked page pointing them at their
 * organizations. The API enforces the same; this keeps the UI honest.
 */
export function AdminOnly({ children }: { children: React.ReactNode }) {
  return (
    <Guard
      role={SITE_ADMIN}
      fallback={
        <PageEmpty
          illustration="locked"
          title="For site administrators"
          description="Organizations, users and access are managed by site administrators. Switch to one of your organizations at the top of the sidebar."
        />
      }
    >
      {children}
    </Guard>
  )
}
