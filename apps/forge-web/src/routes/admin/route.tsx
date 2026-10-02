import { createFileRoute, Outlet } from "@tanstack/react-router"

import { AdminOnly } from "@/features/admin/components/admin-only"
import { useAdminNav } from "@/features/admin/components/admin-nav"

/**
 * Site administration, under /admin: organizations and their members, users,
 * roles and permissions. Only site administrators get in; anyone else gets a
 * locked page (the API enforces the same). None of it is part of an
 * organization's own pages, /organizations/$organizationId. Its pages, and
 * their errors, have its sub nav.
 */
export const Route = createFileRoute("/admin")({
  component: AdminLayout,
})

function AdminLayout() {
  useAdminNav()
  return (
    <AdminOnly>
      <Outlet />
    </AdminOnly>
  )
}
