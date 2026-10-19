import * as React from "react"

import { PanelEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Skeleton } from "@/components/ui/skeleton"
import { OVERVIEW_WIDGETS } from "@/features/overview/components/overview-widgets"
import type { OverviewOrganization } from "@/features/overview/components/widget-builder"
import {
  drawable,
  SHOWS,
  specOf,
  titleOf,
} from "@/features/overview/lib/custom-widget"
import {
  revealWidget,
  useLayoutEditor,
  useLayoutEditorApi,
} from "@/features/overview/lib/layout-editor"
import {
  useRemoveSharedWidget,
  useSharedWidgets,
  withSharedWidget,
  type SharedWidget,
} from "@/features/overview/lib/shared-widgets"
import { parseTimestamp } from "@/lib/timestamps"

/*
 * The widgets an organization shares, while the overview is customized:
 * each can be added (a copy, onto the person's draft layout) and, by whoever
 * shared it or manages them, taken away for everyone after a second click.
 */

const DASHBOARD = "organization-overview"

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" })

/** What a shared widget shows, and who shared it when. */
function describe(widget: SharedWidget) {
  const spec = specOf(widget.settings)
  const what = titleOf(spec)
  const shown = SHOWS[drawable(spec).show]
  const when = dateFormat.format(parseTimestamp(widget.shared_at))
  // Its name, when its title doesn't already say it.
  const kind = what === widget.title ? shown : `${what}, ${shown.toLowerCase()}`
  return `${kind} · ${widget.shared_by_name}, ${when}`
}

export function SharedWidgetsDialog({
  organization,
}: {
  organization: OverviewOrganization
}) {
  const browsing = useLayoutEditor((state) => state.browsing)
  const browse = useLayoutEditor((state) => state.browse)
  return (
    <Dialog open={browsing} onOpenChange={browse}>
      <DialogContent className="gap-5 sm:max-w-xl">
        {/* The popup is there only while open or closing. */}
        <Library organization={organization} />
      </DialogContent>
    </Dialog>
  )
}

function Library({ organization }: { organization: OverviewOrganization }) {
  const list = useSharedWidgets(organization.id, DASHBOARD)
  const remove = useRemoveSharedWidget(organization.id, DASHBOARD)
  const editor = useLayoutEditorApi()
  const [added, setAdded] = React.useState<ReadonlySet<string>>(new Set())
  const [confirming, setConfirming] = React.useState<string | null>(null)

  const add = (widget: SharedWidget) => {
    const { draft, change } = editor.getState()
    if (!draft) return
    const copy = withSharedWidget(draft, widget, OVERVIEW_WIDGETS.custom.w)
    change(copy.widgets)
    revealWidget(copy.id)
    setAdded((last) => new Set(last).add(widget.id))
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>Shared in {organization.name}</DialogTitle>
        <DialogDescription>
          Widgets people shared with {organization.name}. Adding one puts a copy
          on your overview; taking one away leaves the copies people already
          added.
        </DialogDescription>
      </DialogHeader>

      {list.error ? (
        <ErrorCallout
          title="Couldn't load the shared widgets"
          action={
            <Button
              variant="outline"
              size="sm"
              onClick={() => void list.refetch()}
            >
              Retry
            </Button>
          }
        >
          {list.error.message}
        </ErrorCallout>
      ) : list.isPending ? (
        <div aria-busy="true" className="flex flex-col gap-3">
          {Array.from({ length: 3 }, (_, index) => (
            <Skeleton key={index} className="h-9 rounded-(--radius-soft)" />
          ))}
        </div>
      ) : list.data.length === 0 ? (
        <PanelEmpty icon="team" className="py-10">
          Nothing is shared yet. Share a widget from its builder.
        </PanelEmpty>
      ) : (
        <ul
          tabIndex={0}
          aria-label={`Widgets shared in ${organization.name}`}
          className="max-h-[min(28rem,60vh)] overflow-y-auto border-t"
        >
          {list.data.map((widget) => (
            <li
              key={widget.id}
              className="flex items-center gap-3 border-b py-2.5 last:border-b-0"
            >
              <div className="min-w-0 flex-1">
                <p className="truncate text-xs font-medium">{widget.title}</p>
                <p className="truncate text-2xs text-muted-foreground">
                  {describe(widget)}
                </p>
              </div>
              {confirming === widget.id ? (
                <>
                  <Button
                    size="xs"
                    variant="ghost"
                    onClick={() => setConfirming(null)}
                  >
                    Keep
                  </Button>
                  <Button
                    size="xs"
                    variant="destructive"
                    disabled={remove.isPending}
                    onClick={() =>
                      remove.mutate(widget.id, {
                        onSuccess: () => setConfirming(null),
                      })
                    }
                  >
                    Remove for everyone
                  </Button>
                </>
              ) : (
                <>
                  {widget.can_remove && (
                    <Button
                      size="xs"
                      variant="ghost"
                      className="text-muted-foreground"
                      onClick={() => setConfirming(widget.id)}
                    >
                      Remove
                    </Button>
                  )}
                  <Button
                    size="xs"
                    variant="outline"
                    onClick={() => add(widget)}
                  >
                    <Icon
                      icon={added.has(widget.id) ? "check" : "plus"}
                      data-icon="inline-start"
                    />
                    {added.has(widget.id) ? "Added" : "Add"}
                  </Button>
                </>
              )}
            </li>
          ))}
        </ul>
      )}

      <DialogFooter>
        <DialogClose render={<Button variant="outline" />}>Close</DialogClose>
      </DialogFooter>
    </>
  )
}
