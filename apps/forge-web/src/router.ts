import { createRouter } from "@tanstack/react-router"

import { RouteError, RouteNotFound } from "@/components/route-states"
import { queryClient } from "@/lib/api-instance"

import { routeTree } from "./routeTree.gen"

export const router = createRouter({
  routeTree,
  context: { queryClient },
  defaultPreload: "intent",
  // Loaders read through TanStack Query, which owns caching; always call them.
  defaultPreloadStaleTime: 0,
  defaultErrorComponent: RouteError,
  defaultNotFoundComponent: RouteNotFound,
})

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router
  }
  interface StaticDataRouteOption {
    /** The page is the whole window, without the Forge shell (`__root.tsx`). */
    bare?: boolean
  }
}
