import * as React from "react"
import { UserGroupIcon } from "@hugeicons/core-free-icons"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { toast } from "@/components/ui/toast"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { ChartTypeSwitch } from "@/features/overview/components/chart-type-switch"
import { CustomPanel } from "@/features/overview/components/custom-panel"
import { OVERVIEW_WIDGETS } from "@/features/overview/components/overview-widgets"
import { SplitSwitch } from "@/features/overview/components/panels"
import { WidgetTitle } from "@/features/overview/components/widget-title"
import {
  DEFAULT_SPEC,
  drawable,
  linesOnly,
  MEASURE_GROUPS,
  MEASURES,
  ONLY,
  SHOWS,
  specOf,
  SPLITS,
  titleOf,
  type CustomSpec,
  type MeasureKey,
  type OnlyKey,
  type ShowKey,
  type SplitKey,
  withCustomSpec,
  withCustomWidget,
} from "@/features/overview/lib/custom-widget"
import { TITLE_LENGTH } from "@/features/overview/lib/dashboard"
import {
  revealWidget,
  useLayoutEditor,
  useLayoutEditorApi,
} from "@/features/overview/lib/layout-editor"
import { useShareWidget } from "@/features/overview/lib/shared-widgets"
import type { OrganizationOverview } from "@/features/overview/lib/overview"
import type { UsageContext } from "@/features/overview/lib/series"

/*
 * The widget builder, over the overview while it's customized: a person
 * picks what to measure, what to split it by, how to show it and whose work
 * it counts, and sees it drawn from the overview as they go. Saving puts it
 * in their draft layout (Done saves that); a new one goes under the rest.
 * Sharing it puts it in the organization's library at once, for members to
 * add a copy of.
 */

const items = <K extends string>(choices: Record<K, string>) =>
  (Object.keys(choices) as K[]).map((value) => ({
    value,
    label: choices[value],
  }))

const MEASURE_ITEMS = (Object.keys(MEASURES) as MeasureKey[]).map((value) => ({
  value,
  label: MEASURES[value].label,
}))
const SPLIT_ITEMS = (Object.keys(SPLITS) as SplitKey[]).map((value) => ({
  value,
  label: SPLITS[value].label,
}))
const ONLY_ITEMS = items(ONLY)

/** A spec a person changed, kept to what its measure allows. */
function fitted(spec: CustomSpec, change: Partial<CustomSpec>): CustomSpec {
  const next = { ...spec, ...change }
  if (next.split === "model" && !MEASURES[next.measure].byModel) {
    next.split = "none"
  }
  if (next.show === "ranking" && next.split === "none") next.show = "total"
  return next
}

/** The organization the overview is of: where widgets are shared. */
export type OverviewOrganization = { id: string; name: string }

export function WidgetBuilder({
  overview,
  context,
  organization,
}: {
  overview: OrganizationOverview
  context: UsageContext
  organization: OverviewOrganization
}) {
  const building = useLayoutEditor((state) => state.building)
  const stop = useLayoutEditor((state) => state.stopBuilding)
  // The last widget opened stays drawn while the dialog closes.
  const [shown, setShown] = React.useState(building)
  if (building && building !== shown) setShown(building)
  return (
    <Dialog
      open={building !== null}
      onOpenChange={(open) => {
        if (!open) stop()
      }}
    >
      <DialogContent className="gap-5 sm:max-w-[54rem]">
        {shown && (
          <BuilderBody
            key={shown.id ?? "new"}
            id={shown.id}
            overview={overview}
            context={context}
            organization={organization}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

function BuilderBody({
  id,
  overview,
  context,
  organization,
}: {
  /** The widget being changed; null for a new one. */
  id: string | null
  overview: OrganizationOverview
  context: UsageContext
  organization: OverviewOrganization
}) {
  const editor = useLayoutEditorApi()
  const formId = React.useId()
  const [start] = React.useState(() => {
    const widget = id
      ? editor.getState().draft?.find((found) => found.id === id)
      : undefined
    return {
      spec: widget ? specOf(widget.settings ?? {}) : DEFAULT_SPEC,
      title: widget?.title ?? "",
    }
  })
  const [spec, setSpec] = React.useState(start.spec)
  const [title, setTitle] = React.useState(start.title)
  const update = (change: Partial<CustomSpec>) =>
    setSpec((last) => fitted(last, change))

  const measure = MEASURES[spec.measure]
  const name = titleOf(spec)
  const own = title.trim()

  const save = () => {
    const { draft, change, stopBuilding } = editor.getState()
    if (!draft) return
    if (id === null) {
      const added = withCustomWidget(
        draft,
        spec,
        own,
        OVERVIEW_WIDGETS.custom.w
      )
      change(added.widgets)
      revealWidget(added.id)
    } else {
      change(withCustomSpec(draft, id, spec, own))
    }
    stopBuilding()
  }

  // Shared as it is now: changed again, it can be shared again.
  const share = useShareWidget(organization.id, "organization-overview")
  const definition = JSON.stringify([spec, own || name])
  const [shared, setShared] = React.useState<string | null>(null)
  const shareIt = () =>
    share.mutate(
      { type: "custom", title: own || name, settings: spec },
      {
        onSuccess: () => {
          setShared(definition)
          toast.add({
            title: `Shared with ${organization.name}`,
            description: "Its members can add it from Add widget.",
            type: "success",
          })
        },
      }
    )

  return (
    <form
      className="contents"
      onSubmit={(event) => {
        event.preventDefault()
        save()
      }}
    >
      <DialogHeader>
        <DialogTitle>{id ? "Change widget" : "New widget"}</DialogTitle>
        <DialogDescription>
          Pick what it measures and how it shows it. It reads the overview
          you're looking at and follows what you pick in the ledger.
        </DialogDescription>
      </DialogHeader>

      <div className="grid grid-cols-[minmax(0,17rem)_minmax(0,1fr)] gap-6 max-[720px]:grid-cols-1">
        <FieldGroup className="gap-4">
          <Field>
            <FieldLabel htmlFor={`${formId}-title`}>Title</FieldLabel>
            <Input
              id={`${formId}-title`}
              value={title}
              placeholder={name}
              maxLength={TITLE_LENGTH}
              onChange={(event) => setTitle(event.target.value)}
            />
          </Field>

          <Field>
            <FieldLabel htmlFor={`${formId}-measure`}>Measure</FieldLabel>
            <Select
              items={MEASURE_ITEMS}
              value={spec.measure}
              onValueChange={(value) =>
                value && update({ measure: value as MeasureKey })
              }
            >
              <SelectTrigger id={`${formId}-measure`} className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent alignItemWithTrigger={false}>
                {(
                  Object.keys(MEASURE_GROUPS) as (keyof typeof MEASURE_GROUPS)[]
                ).map((group) => (
                  <SelectGroup key={group}>
                    <SelectLabel>{MEASURE_GROUPS[group]}</SelectLabel>
                    {MEASURE_ITEMS.filter(
                      (item) => MEASURES[item.value].group === group
                    ).map((item) => (
                      <SelectItem key={item.value} value={item.value}>
                        {item.label}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                ))}
              </SelectContent>
            </Select>
          </Field>

          <Field>
            <FieldLabel htmlFor={`${formId}-split`}>Split by</FieldLabel>
            <Select
              items={SPLIT_ITEMS}
              value={spec.split}
              onValueChange={(value) =>
                value && update({ split: value as SplitKey })
              }
            >
              <SelectTrigger id={`${formId}-split`} className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent alignItemWithTrigger={false}>
                {SPLIT_ITEMS.map((item) => (
                  <SelectItem
                    key={item.value}
                    value={item.value}
                    disabled={item.value === "model" && !measure.byModel}
                  >
                    {item.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {!measure.byModel && (
              <FieldDescription>
                Runs don't have a model, so they don't split by one.
              </FieldDescription>
            )}
          </Field>

          <Field>
            <FieldLabel>Show as</FieldLabel>
            <SplitSwitch<ShowKey>
              label="Show as"
              // A ranking needs something to rank.
              options={
                (spec.split === "none"
                  ? { time: SHOWS.time, total: SHOWS.total }
                  : SHOWS) as Record<ShowKey, string>
              }
              value={spec.show}
              onValueChange={(show) => update({ show })}
            />
            {spec.split === "none" && (
              <FieldDescription>
                Split it by something to rank what it's split by.
              </FieldDescription>
            )}
          </Field>

          {spec.show === "time" && (
            <Field>
              <FieldLabel>Chart</FieldLabel>
              <ChartTypeSwitch
                value={drawable(spec).chart}
                disabled={linesOnly(spec)}
                onChange={(chart) => update({ chart })}
              />
              {linesOnly(spec) && (
                <FieldDescription>
                  A rate split into series doesn't stack, so it's drawn as
                  lines.
                </FieldDescription>
              )}
            </Field>
          )}

          <Field>
            <FieldLabel htmlFor={`${formId}-only`}>Counting</FieldLabel>
            <Select
              items={ONLY_ITEMS}
              value={spec.only}
              onValueChange={(value) =>
                value && update({ only: value as OnlyKey })
              }
            >
              <SelectTrigger id={`${formId}-only`} className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent alignItemWithTrigger={false}>
                {ONLY_ITEMS.map((item) => (
                  <SelectItem key={item.value} value={item.value}>
                    {item.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
        </FieldGroup>

        <section
          aria-label="Preview"
          className="min-w-0 self-start rounded-(--radius-card) border bg-card px-4 pt-4 pb-2"
        >
          <WidgetTitle value={own || undefined}>
            <CustomPanel overview={overview} context={context} spec={spec} />
          </WidgetTitle>
        </section>
      </div>

      <DialogFooter>
        <Button
          type="button"
          variant="ghost"
          className="sm:mr-auto"
          disabled={share.isPending || shared === definition}
          onClick={shareIt}
        >
          {share.isPending ? (
            <Spinner data-icon="inline-start" />
          ) : (
            <Icon
              icon={shared === definition ? "check" : UserGroupIcon}
              data-icon="inline-start"
            />
          )}
          {shared === definition
            ? `Shared with ${organization.name}`
            : `Share with ${organization.name}`}
        </Button>
        <Button
          type="button"
          variant="outline"
          onClick={() => editor.getState().stopBuilding()}
        >
          Cancel
        </Button>
        <Button type="submit">{id ? "Save widget" : "Add widget"}</Button>
      </DialogFooter>
    </form>
  )
}
