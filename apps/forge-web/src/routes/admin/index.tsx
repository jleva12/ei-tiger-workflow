import { createFileRoute, Navigate } from "@tanstack/react-router"

/** /admin opens its first page, Organizations. */
export const Route = createFileRoute("/admin/")({
  component: () => <Navigate to="/admin/organizations" replace />,
})
