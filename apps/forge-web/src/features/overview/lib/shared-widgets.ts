import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { specOf, withCustomWidget } from "@/features/overview/lib/custom-widget"
import type {
  DashboardId,
  DashboardWidget,
} from "@/features/overview/lib/dashboard"
import { api } from "@/lib/api-instance"

/*
 * Widgets people share with their organization: the library members add
 * them from, each taking a copy into their own layout
 * (/organizations/{org}/dashboards/{dashboard}/shared-widgets). Its sharer
 * may take one away, and so may its administrators (shared_widgets:manage);
 * the copies stay where they were added.
 */

export type SharedWidget = {
  id: string
  type: "custom"
  title: string
  settings: Record<string, unknown>
  shared_by: string
  shared_by_name: string
  shared_at: string
  /** The caller shared it, or manages the organization's shared widgets. */
  can_remove: boolean
}

export const sharedWidgetKeys = {
  all: ["shared-widgets"] as const,
  of: (organizationId: string, dashboard: DashboardId) =>
    [...sharedWidgetKeys.all, organizationId, dashboard] as const,
}

const pathOf = (organizationId: string, dashboard: DashboardId) =>
  `/organizations/${encodeURIComponent(organizationId)}/dashboards/${encodeURIComponent(dashboard)}/shared-widgets`

/** The widgets shared with an organization for a dashboard, newest first. */
export function useSharedWidgets(
  organizationId: string,
  dashboard: DashboardId,
  enabled = true
) {
  return useQuery({
    queryKey: sharedWidgetKeys.of(organizationId, dashboard),
    queryFn: ({ signal }) =>
      api.get<SharedWidget[]>(pathOf(organizationId, dashboard), { signal }),
    enabled,
  })
}

/** Share a widget with an organization. */
export function useShareWidget(organizationId: string, dashboard: DashboardId) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (widget: {
      type: "custom"
      title: string
      settings: Record<string, unknown>
    }) => api.post<SharedWidget>(pathOf(organizationId, dashboard), widget),
    meta: { errorTitle: "Couldn't share the widget" },
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: sharedWidgetKeys.of(organizationId, dashboard),
      }),
  })
}

/** Take a shared widget away from an organization. */
export function useRemoveSharedWidget(
  organizationId: string,
  dashboard: DashboardId
) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) =>
      api.delete<void>(
        `${pathOf(organizationId, dashboard)}/${encodeURIComponent(id)}`
      ),
    meta: { errorTitle: "Couldn't take the widget away" },
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: sharedWidgetKeys.of(organizationId, dashboard),
      }),
  })
}

/** A copy of a shared widget, added under the rest of a layout; and its ID. */
export const withSharedWidget = (
  widgets: readonly DashboardWidget[],
  shared: SharedWidget,
  w: number
) => withCustomWidget(widgets, specOf(shared.settings), shared.title, w)
