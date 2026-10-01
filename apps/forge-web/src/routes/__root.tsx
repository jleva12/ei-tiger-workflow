import type { QueryClient } from "@tanstack/react-query"
import {
  Outlet,
  createRootRouteWithContext,
  useMatches,
} from "@tanstack/react-router"

import { AppLayout } from "@/components/app-layout"
import { AppError } from "@/components/route-states"
import { isSettingsSection, type SettingsSearch } from "@/lib/settings"

export type RouterContext = {
  /** For loaders: `context.queryClient.ensureQueryData(tasks.detailOptions(id))`. */
  queryClient: QueryClient
}

export const Route = createRootRouteWithContext<RouterContext>()({
  // Every page can have Settings open over it (settings-dialog.tsx).
  validateSearch: (search: Record<string, unknown>): SettingsSearch => ({
    settings: isSettingsSection(search.settings) ? search.settings : undefined,
  }),
  component: RootLayout,
  // Replaces the shell, so it can't rely on it; pages below get RouteError.
  errorComponent: AppError,
})

/**
 * Pages in the Forge shell; a route with `staticData: { bare: true }`, like
 * the assistant's own window, is the whole window instead.
 */
function RootLayout() {
  const bare = useMatches({
    select: (matches) => matches.some((match) => match.staticData.bare),
  })
  return bare ? <Outlet /> : <AppLayout />
}
