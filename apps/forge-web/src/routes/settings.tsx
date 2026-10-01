import { createFileRoute, redirect } from "@tanstack/react-router"

/**
 * Settings is a dialog over any page (settings-dialog.tsx); links and
 * bookmarks to /settings open it over Home.
 */
export const Route = createFileRoute("/settings")({
  beforeLoad: () => {
    throw redirect({
      to: "/",
      search: { settings: "display" },
      replace: true,
    })
  },
})
