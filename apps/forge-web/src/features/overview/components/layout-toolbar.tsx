import { DashboardSquareEditIcon } from "@hugeicons/core-free-icons"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Spinner } from "@/components/ui/spinner"
import { OVERVIEW_WIDGETS } from "@/features/overview/components/overview-widgets"
import type { OverviewOrganization } from "@/features/overview/components/widget-builder"
import {
  LAYOUT_VERSION,
  OVERVIEW_LAYOUT,
  OVERVIEW_WIDGET_TYPES,
  sameWidgets,
  useResetLayout,
  useSavedLayout,
  useSaveLayout,
  withWidget,
  type DashboardWidget,
  type OverviewWidgetType,
} from "@/features/overview/lib/dashboard"
import {
  revealWidget,
  useLayoutEditor,
  useLayoutEditorApi,
} from "@/features/overview/lib/layout-editor"
import {
  useSharedWidgets,
  withSharedWidget,
  type SharedWidget,
} from "@/features/overview/lib/shared-widgets"

/*
 * Customizing the overview's panels: Customize starts from what it shows,
 * then widgets are added (the overview's own, or new ones a person defines
 * in the widget builder) or the default put back, and Done saves the layout
 * for the person, in every organization, or Cancel drops it. A layout that's
 * the default again is forgotten rather than saved, so it keeps up with the
 * default as that changes. Under 900px of shell the panels stack and can't
 * be arranged, so there's no Customize.
 */

const DASHBOARD = "organization-overview"

/** How many shared widgets the menu offers before its library. */
const MENU_SHARED = 6

/** Start customizing from the layout the overview shows. */
export function CustomizeButton({ widgets }: { widgets: DashboardWidget[] }) {
  const edit = useLayoutEditor((state) => state.edit)
  return (
    <Button
      variant="ghost"
      size="xs"
      className="text-muted-foreground @max-[900px]/shell:hidden"
      onClick={() => edit(widgets)}
    >
      <Icon icon={DashboardSquareEditIcon} data-icon="inline-start" />
      Customize
    </Button>
  )
}

/** What customizing offers: add a widget, the default back, Cancel, Done. */
export function LayoutToolbar({
  widgets,
  organization,
}: {
  /** The layout the overview shows when it isn't customized. */
  widgets: DashboardWidget[]
  /** Whose overview it is, and so whose shared widgets it offers. */
  organization: OverviewOrganization
}) {
  const draft = useLayoutEditor((state) => state.draft)
  const editor = useLayoutEditorApi()
  const saved = useSavedLayout(DASHBOARD)
  const save = useSaveLayout(DASHBOARD)
  const reset = useResetLayout(DASHBOARD)
  const shared = useSharedWidgets(organization.id, DASHBOARD)
  if (!draft) return null

  const addShared = (widget: SharedWidget) => {
    const copy = withSharedWidget(draft, widget, OVERVIEW_WIDGETS.custom.w)
    editor.getState().change(copy.widgets)
    revealWidget(copy.id)
  }

  const busy = save.isPending || reset.isPending
  const missing = OVERVIEW_WIDGET_TYPES.filter(
    (type) => !draft.some((widget) => widget.type === type)
  )

  const add = (type: OverviewWidgetType) => {
    const next = withWidget(draft, type, OVERVIEW_WIDGETS[type].w)
    editor.getState().change(next)
    // Added under the rest: once it's drawn, bring it into view.
    revealWidget(next[next.length - 1].id)
  }

  const done = async () => {
    const { draft: current, close } = editor.getState()
    if (!current) return
    try {
      if (sameWidgets(current, OVERVIEW_LAYOUT.widgets)) {
        if (saved.data?.layout != null) await reset.mutateAsync()
      } else if (!sameWidgets(current, widgets)) {
        await save.mutateAsync({ version: LAYOUT_VERSION, widgets: current })
      }
      close()
    } catch {
      // The query client says what failed; the draft stays to try again.
    }
  }

  return (
    <div className="flex flex-wrap items-center justify-end gap-1">
      <DropdownMenu>
        <DropdownMenuTrigger
          render={<Button variant="outline" size="xs" disabled={busy} />}
        >
          <Icon icon="plus" data-icon="inline-start" />
          Add widget
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-72">
          <DropdownMenuGroup>
            <DropdownMenuLabel>Widgets not on the overview</DropdownMenuLabel>
            {missing.length === 0 ? (
              <DropdownMenuItem disabled>
                Every widget is on it
              </DropdownMenuItem>
            ) : (
              missing.map((type) => (
                <DropdownMenuItem key={type} onClick={() => add(type)}>
                  {OVERVIEW_WIDGETS[type].title}
                </DropdownMenuItem>
              ))
            )}
          </DropdownMenuGroup>
          <DropdownMenuSeparator />
          <DropdownMenuGroup>
            <DropdownMenuLabel>Shared in {organization.name}</DropdownMenuLabel>
            {shared.error ? (
              <DropdownMenuItem disabled>
                Couldn't load the shared widgets
              </DropdownMenuItem>
            ) : shared.isPending ? (
              <DropdownMenuItem disabled>Loading…</DropdownMenuItem>
            ) : shared.data.length === 0 ? (
              <DropdownMenuItem disabled>Nothing shared yet</DropdownMenuItem>
            ) : (
              shared.data.slice(0, MENU_SHARED).map((widget) => (
                <DropdownMenuItem
                  key={widget.id}
                  onClick={() => addShared(widget)}
                >
                  <span className="min-w-0 flex-1 truncate">
                    {widget.title}
                  </span>
                  <span className="max-w-[40%] shrink-0 truncate text-2xs text-muted-foreground">
                    {widget.shared_by_name}
                  </span>
                </DropdownMenuItem>
              ))
            )}
            {(shared.data?.length ?? 0) > 0 && (
              <DropdownMenuItem onClick={() => editor.getState().browse(true)}>
                <Icon icon="team" />
                All shared widgets…
              </DropdownMenuItem>
            )}
          </DropdownMenuGroup>
          <DropdownMenuSeparator />
          <DropdownMenuItem onClick={() => editor.getState().build(null)}>
            <Icon icon="plus" />
            New widget…
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <Button
        variant="ghost"
        size="xs"
        disabled={busy || sameWidgets(draft, OVERVIEW_LAYOUT.widgets)}
        onClick={() => editor.getState().change(OVERVIEW_LAYOUT.widgets)}
      >
        Reset to default
      </Button>
      <Button
        variant="ghost"
        size="xs"
        disabled={busy}
        onClick={() => editor.getState().close()}
      >
        Cancel
      </Button>
      <Button size="xs" disabled={busy} onClick={() => void done()}>
        {busy && <Spinner data-icon="inline-start" />}
        Done
      </Button>
    </div>
  )
}
