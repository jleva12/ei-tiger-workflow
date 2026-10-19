import * as React from "react"
import { Link } from "@tanstack/react-router"
import { cn } from "cn"

import { Footnote, Footnotes } from "@/components/forge/activity"
import { PageEmpty, PanelEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { slotsBy } from "@/features/overview/components/chart-kit"
import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button"
import { DashboardGrid } from "@/features/overview/components/dashboard-grid"
import {
  CustomizeButton,
  LayoutToolbar,
} from "@/features/overview/components/layout-toolbar"
import {
  type KindFilter,
  type LedgerFocus,
} from "@/features/overview/components/overview-summary"
import { SCOPE_ROW } from "@/features/overview/lib/ledger"
import { ChartTypeSwitch } from "@/features/overview/components/chart-type-switch"
import {
  CHART_SETTINGS,
  OVERVIEW_WIDGETS,
  type OverviewCatalogType,
} from "@/features/overview/components/overview-widgets"
import { OverviewSkeleton } from "@/features/overview/components/panels"
import {
  OVERVIEW_LAYOUT,
  resolveLayout,
  useSavedLayout,
  type DashboardLayout,
  type DashboardWidget,
  withSetting,
} from "@/features/overview/lib/dashboard"
import { SharedWidgetsDialog } from "@/features/overview/components/shared-widgets-dialog"
import { WidgetBuilder } from "@/features/overview/components/widget-builder"
import {
  chartTypeOf,
  DEFAULT_CHART_TYPE,
  type ChartType,
} from "@/features/overview/lib/chart-type"
import {
  CUSTOM_SETTINGS,
  linesOnly,
  specOf,
} from "@/features/overview/lib/custom-widget"
import {
  LayoutEditorProvider,
  useLayoutEditor,
} from "@/features/overview/lib/layout-editor"
import {
  spanOf,
  type OverviewView,
} from "@/features/overview/lib/overview-view"
import { sharedTicks } from "@/features/overview/lib/format"
import {
  periodWords,
  SUBJECT_KINDS,
  summarize,
  useOrganizationOverview,
  weeksBySubject,
  type OrganizationOverview as Overview,
  type OverviewScope,
  type SubjectKind,
} from "@/features/overview/lib/overview"
import type { UsageContext } from "@/features/overview/lib/series"
import { parseTimestamp } from "@/lib/timestamps"

/*
 * An organization's overview, for the people who run it: a sentence on the
 * period, its workflows and agents compared in a ledger, then its tokens
 * (the headline), models and spend, what ran and how it ended, who ran it,
 * what in it used the tokens, and what needs a person now. Selecting a row
 * in the ledger, or a type, focuses every other widget on it. Each of them
 * is a widget, laid out as the person saved them, or by default; Customize
 * lets them move, size, rename, add and remove every one.
 */

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" })

export function OrganizationOverview({
  organizationId,
  organizationName,
  view,
  onView,
  onOpenRun,
}: {
  organizationId: string
  organizationName: string
  /** Its period and focus, as the page's address keeps them. */
  view: OverviewView
  onView: (change: Partial<OverviewView>) => void
  /** Open a workflow run's page; without it, waiting runs are listed only. */
  onOpenRun?: (runId: string) => void
}) {
  const query = useOrganizationOverview(organizationId, spanOf(view))
  const overview = query.data
  const saved = useSavedLayout("organization-overview")
  const layout = React.useMemo(
    () => resolveLayout(saved.data?.layout, OVERVIEW_WIDGETS, OVERVIEW_LAYOUT),
    [saved.data]
  )

  if (query.error && !overview) {
    return (
      <ErrorCallout
        title={`Couldn't load ${organizationName}'s overview`}
        action={
          <Button
            variant="outline"
            size="sm"
            onClick={() => void query.refetch()}
          >
            Retry
          </Button>
        }
      >
        {query.error.message}
      </ErrorCallout>
    )
  }
  // Waits for the layout too, so a saved one doesn't replace the default in view.
  if (!overview || saved.isPending) return <OverviewSkeleton />
  if (overview.subjects.length === 0) {
    return (
      <PageEmpty
        illustration="activity"
        title="Nothing to report on yet"
        description={`The overview compares ${organizationName}'s workflows and agents: how often they run, how it ends, the model tokens they use and what they cost. Build a workflow or an agent and run it, and it shows here.`}
      />
    )
  }
  return (
    // Above the report, so a draft layout outlasts a new period.
    <LayoutEditorProvider>
      <Report
        key={`${overview.period}:${overview.first_day}:${overview.last_day}`}
        organizationId={organizationId}
        organizationName={organizationName}
        overview={overview}
        view={view}
        onView={onView}
        layout={layout}
        // A new period loads behind the last one; fade it while it does.
        stale={query.isPlaceholderData}
        onOpenRun={onOpenRun}
      />
    </LayoutEditorProvider>
  )
}

function Report({
  organizationId,
  organizationName,
  overview,
  view,
  onView,
  layout,
  stale,
  onOpenRun,
}: {
  organizationId: string
  organizationName: string
  overview: Overview
  view: OverviewView
  onView: (change: Partial<OverviewView>) => void
  layout: DashboardLayout
  stale: boolean
  onOpenRun?: (runId: string) => void
}) {
  // The focus is the page's: the ledger, and the header's picker, change it.
  const kind: KindFilter = view.only ?? "all"
  const focus = view.focus ?? null
  const draft = useLayoutEditor((state) => state.draft)
  const change = useLayoutEditor((state) => state.change)
  const widgets = draft ?? layout.widgets
  const organization = React.useMemo(
    () => ({ id: organizationId, name: organizationName }),
    [organizationId, organizationName]
  )
  const now = parseTimestamp(overview.as_of).getTime()
  const subjects = React.useMemo(
    () => new Map(overview.subjects.map((subject) => [subject.key, subject])),
    [overview.subjects]
  )
  const focused = focus ? subjects.get(focus) : undefined
  const focusKey = focused?.key
  const scope = React.useMemo(
    (): OverviewScope =>
      focusKey
        ? { level: "subject", key: focusKey }
        : kind === "all"
          ? { level: "organization" }
          : { level: "kind", kind },
    [focusKey, kind]
  )

  // Colour slots by tokens over the last 12 weeks, so each keeps its colour
  // whatever the period or the focus.
  const weeks = React.useMemo(
    () => weeksBySubject(overview, "tokens"),
    [overview]
  )
  const slots = React.useMemo(
    () =>
      slotsBy(
        overview.subjects,
        (subject) => subject.key,
        (subject) => (weeks.get(subject.key) ?? []).reduce((a, b) => a + b, 0),
        (subject) => subject.name
      ),
    [overview.subjects, weeks]
  )
  const summary = React.useMemo(
    () => summarize(overview, scope),
    [overview, scope]
  )
  const unit = overview.unit
  const ticks = sharedTicks(
    summary.buckets.map((bucket) => bucket.start),
    unit
  )
  const context: UsageContext = { summary, scope, unit, ticks, slots, subjects }
  const kinds = (["workflow", "agent", "assistant"] as SubjectKind[]).filter(
    (k) => overview.subjects.some((subject) => subject.kind === k)
  )

  const select = (id: string) =>
    onView({ focus: id === SCOPE_ROW || id === focus ? undefined : id })
  const chooseKind = (next: KindFilter) =>
    onView({
      only: next === "all" ? undefined : next,
      ...(focused &&
        next !== "all" &&
        focused.kind !== next && { focus: undefined }),
    })
  const ledger: LedgerFocus = {
    kind,
    kinds,
    focus,
    weeks,
    onKind: chooseKind,
    onSelect: select,
  }
  const scopeKey = `${scope.level}:${focus ?? kind}`

  return (
    // Phones keep the last footnote clear of the assistant launcher.
    <div
      data-slot="organization-overview"
      className={cn(
        "flex flex-col transition-opacity @max-[600px]/shell:pb-16",
        stale && "opacity-60"
      )}
    >
      <section id="overview-scope" aria-labelledby="overview-scope-title">
        <header className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-1">
          <Icon icon="dashboard" size={16} className="text-muted-foreground" />
          <h2 id="overview-scope-title" className="text-sm font-medium">
            {focused
              ? focused.name
              : kind === "all"
                ? "All workflows and agents"
                : `All ${SUBJECT_KINDS[kind].plural.toLowerCase()}`}
          </h2>
          <span className="text-xs text-muted-foreground">
            {periodWords(overview).label}
          </span>
          <span className="ml-auto flex items-center gap-1">
            {draft ? (
              <LayoutToolbar
                widgets={layout.widgets}
                organization={organization}
              />
            ) : (
              <>
                {focused && (
                  <>
                    <OpenSubject
                      organizationId={organizationId}
                      subject={focused}
                    />
                    <Button
                      variant="ghost"
                      size="xs"
                      className="text-muted-foreground"
                      onClick={() => onView({ focus: undefined })}
                    >
                      <Icon icon="close" data-icon="inline-start" />
                      Back to all
                    </Button>
                  </>
                )}
                <CustomizeButton widgets={layout.widgets} />
              </>
            )}
          </span>
        </header>
        {draft && (
          <p className="-mt-2 mb-4 text-xs text-muted-foreground">
            Drag a widget by its bar, size it from its edges or corner, and
            rename or remove it there. Done saves this layout for you, in every
            organization.
          </p>
        )}

        {overview.truncated && (
          <ErrorCallout
            className="mb-5"
            icon="warning"
            title="Only the latest runs are counted"
          >
            This organization started more workflow runs in the period than the
            overview reads at once; the oldest are left out of the figures.
          </ErrorCallout>
        )}

        {widgets.length === 0 ? (
          <PanelEmpty icon="dashboard" className="border-t">
            {draft
              ? "No widgets. Add one to show it here."
              : "No widgets on your overview. Customize it to add some."}
          </PanelEmpty>
        ) : (
          <DashboardGrid
            widgets={widgets}
            bare={(widget) =>
              Boolean(OVERVIEW_WIDGETS[widget.type as OverviewCatalogType].bare)
            }
            render={(widget) => {
              const kind = OVERVIEW_WIDGETS[widget.type as OverviewCatalogType]
              const drawn = kind.render({
                overview,
                context,
                now,
                onOpenRun,
                settings: widget.settings ?? {},
                ledger,
              })
              // What follows the focus fades in when it changes, so the
              // change reads as one; the read-out and the ledger stay put.
              return kind.bare ? (
                drawn
              ) : (
                <div
                  key={scopeKey}
                  className="flex min-w-0 animate-in flex-col duration-150 fade-in-0"
                >
                  {drawn}
                </div>
              )
            }}
            editor={
              draft
                ? {
                    catalog: OVERVIEW_WIDGETS,
                    onChange: change,
                    controls: (widget) => (
                      <WidgetControls
                        widget={widget}
                        onChart={(type) =>
                          change(
                            withSetting(
                              draft,
                              widget.id,
                              "chart",
                              type,
                              DEFAULT_CHART_TYPE
                            )
                          )
                        }
                      />
                    ),
                  }
                : undefined
            }
          />
        )}
        {draft && (
          <>
            <WidgetBuilder
              overview={overview}
              context={context}
              organization={organization}
            />
            <SharedWidgetsDialog organization={organization} />
          </>
        )}
      </section>

      <Footnotes className="mt-10 max-w-[680px]">
        <Footnote icon="coins">
          Tokens and spend are every model call the organization's workflows,
          agents and assistant made, counted in the period it was made. Input
          includes cached input, and output includes thinking. Spend is priced
          from each model's cost in the model provider configuration; calls on
          models without one aren't counted.
        </Footnote>
        <Footnote icon="activity">
          A workflow run counts in the period it started, by how it has ended so
          far. An agent's invocation is one turn of a conversation, from the
          API, a test in its builder or a workflow; the assistant counts when
          asked from the organization's pages.
        </Footnote>
        <Footnote icon="clock">
          Days are yours ({overview.time_zone}). Changes compare with the{" "}
          {periodWords(overview).length} before. The small bars count each
          week's tokens over the last 12 weeks; the last is this week so far.
          Colours rank workflows and agents by tokens over those weeks, so each
          keeps its colour whatever the period.
        </Footnote>
        <Footnote icon="info">
          {overview.recording_since
            ? `Model usage has been recorded since ${dateFormat.format(parseTimestamp(overview.recording_since))}.`
            : "Model usage is recorded from the next model call on."}
        </Footnote>
      </Footnotes>
    </div>
  )
}

/**
 * A widget's own settings in its bar while customizing: a chart panel's
 * type; for a widget a person defined, its chart's type (when it's drawn
 * over time and isn't fixed to lines) and the builder to change it.
 */
function WidgetControls({
  widget,
  onChart,
}: {
  widget: DashboardWidget
  onChart: (type: ChartType) => void
}) {
  const build = useLayoutEditor((state) => state.build)
  const settings = widget.settings ?? {}
  const kind = OVERVIEW_WIDGETS[widget.type as OverviewCatalogType]
  if (kind.settings === CHART_SETTINGS) {
    return <ChartTypeSwitch value={chartTypeOf(settings)} onChange={onChart} />
  }
  if (kind.settings !== CUSTOM_SETTINGS) return null
  const spec = specOf(settings)
  return (
    <>
      {spec.show === "time" && !linesOnly(spec) && (
        <ChartTypeSwitch value={spec.chart} onChange={onChart} />
      )}
      <TooltipIconButton
        tooltip="Change widget"
        side="top"
        onClick={() => build(widget.id)}
        className="size-6 text-muted-foreground hover:text-foreground"
      >
        <Icon icon="settings" size={14} />
      </TooltipIconButton>
    </>
  )
}

/** A link to the focused workflow's or agent's builder, when it's still there. */
function OpenSubject({
  organizationId,
  subject,
}: {
  organizationId: string
  subject: { kind: SubjectKind; id: string; current: boolean }
}) {
  if (!subject.current || subject.kind === "assistant") return null
  const link =
    subject.kind === "workflow" ? (
      <Link
        to="/organizations/$organizationId/agents/$agentId"
        params={{ organizationId, agentId: subject.id }}
      />
    ) : (
      <Link
        to="/organizations/$organizationId/chat-agents/$chatAgentId"
        params={{ organizationId, chatAgentId: subject.id }}
      />
    )
  return (
    <Button
      variant="ghost"
      size="xs"
      className="text-muted-foreground"
      nativeButton={false}
      render={link}
    >
      <Icon icon="external" data-icon="inline-start" />
      Open {SUBJECT_KINDS[subject.kind].label.toLowerCase()}
    </Button>
  )
}
