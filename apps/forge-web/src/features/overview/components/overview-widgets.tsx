import type * as React from "react"
import { z } from "zod"

import {
  ActivityPanel,
  AttentionPanel,
  OutcomesPanel,
  PeoplePanel,
  StepsPanel,
} from "@/features/overview/components/activity-panels"
import { CustomPanel } from "@/features/overview/components/custom-panel"
import {
  OverviewLedger,
  ReadOut,
  type LedgerFocus,
} from "@/features/overview/components/overview-summary"
import {
  ModelsPanel,
  SpendPanel,
  TokensPanel,
} from "@/features/overview/components/usage-panels"
import {
  CHART_TYPES,
  ChartTypeContext,
  chartTypeOf,
  DEFAULT_CHART_TYPE,
} from "@/features/overview/lib/chart-type"
import { CUSTOM_SETTINGS, specOf } from "@/features/overview/lib/custom-widget"
import type {
  OverviewWidgetType,
  WidgetSpec,
} from "@/features/overview/lib/dashboard"
import type { OrganizationOverview } from "@/features/overview/lib/overview"
import type { UsageContext } from "@/features/overview/lib/series"

/*
 * The widgets an organization's overview can show, by the type a layout
 * names them with: the read-out sentence, the ledger of workflows and agents
 * (whose focus every other widget follows), and the panels. The panels with
 * a chart over the period let a person pick its type: bars, area or line. Beside them, people define
 * widgets of their own (`custom`), as many as they like.
 */

/** What every overview widget draws from. */
export type OverviewWidgetProps = {
  overview: OrganizationOverview
  /** The focused scope's sums, colours and dates. */
  context: UsageContext
  /** The overview's as-of time, in milliseconds. */
  now: number
  /** Open a workflow run's page; without it, runs are listed only. */
  onOpenRun?: (runId: string) => void
  /** Its settings, read against its type's. */
  settings: Record<string, unknown>
  /** The ledger's focus, for the ledger to show and change. */
  ledger: LedgerFocus
}

export type OverviewWidget = WidgetSpec & {
  /** Its name, as its panel titles it. */
  title: string
  /** Its width in columns when it's added. */
  w: number
  /** It draws its own frame: the grid adds no hairlines or padding. */
  bare?: boolean
  /** It has no title to rename. */
  untitled?: boolean
  render: (props: OverviewWidgetProps) => React.ReactNode
}

/** The settings of a widget with a chart over the period. */
export const CHART_SETTINGS = z.object({
  chart: z.enum(CHART_TYPES).default(DEFAULT_CHART_TYPE),
})

/** A chart panel, drawn in the type the widget's settings pick. */
const charted =
  (draw: (context: UsageContext) => React.ReactNode) =>
  ({ context, settings }: OverviewWidgetProps) => (
    <ChartTypeContext value={chartTypeOf(settings)}>
      {draw(context)}
    </ChartTypeContext>
  )

/** Every type a layout may name: the panels, and the widgets people define. */
export type OverviewCatalogType = OverviewWidgetType | "custom"

export const OVERVIEW_WIDGETS: Record<OverviewCatalogType, OverviewWidget> = {
  readout: {
    title: "Summary",
    minW: 4,
    w: 12,
    bare: true,
    untitled: true,
    render: ({ overview, now }) => <ReadOut overview={overview} now={now} />,
  },
  ledger: {
    title: "Workflows and agents",
    minW: 6,
    w: 12,
    bare: true,
    render: ({ overview, context, now, ledger }) => (
      <OverviewLedger
        overview={overview}
        kind={ledger.kind}
        kinds={ledger.kinds}
        focus={ledger.focus}
        slots={context.slots}
        weeks={ledger.weeks}
        now={now}
        onKind={ledger.onKind}
        onSelect={ledger.onSelect}
      />
    ),
  },
  custom: {
    title: "Custom widget",
    minW: 3,
    w: 4,
    settings: CUSTOM_SETTINGS,
    render: ({ overview, context, settings }) => (
      <CustomPanel
        overview={overview}
        context={context}
        spec={specOf(settings)}
      />
    ),
  },
  tokens: {
    title: "Model tokens",
    minW: 4,
    w: 8,
    settings: CHART_SETTINGS,
    render: charted((context) => <TokensPanel context={context} />),
  },
  models: {
    title: "Models",
    minW: 3,
    w: 4,
    render: ({ context }) => <ModelsPanel context={context} />,
  },
  activity: {
    title: "Runs and invocations",
    minW: 3,
    w: 4,
    settings: CHART_SETTINGS,
    render: charted((context) => <ActivityPanel context={context} />),
  },
  outcomes: {
    title: "Outcomes",
    minW: 3,
    w: 4,
    settings: CHART_SETTINGS,
    render: charted((context) => <OutcomesPanel context={context} />),
  },
  spend: {
    title: "Model spend",
    minW: 3,
    w: 4,
    settings: CHART_SETTINGS,
    render: charted((context) => <SpendPanel context={context} />),
  },
  people: {
    title: "People",
    minW: 3,
    w: 4,
    render: ({ overview, context }) => (
      <PeoplePanel overview={overview} context={context} />
    ),
  },
  steps: {
    title: "Steps and tools",
    minW: 3,
    w: 4,
    render: ({ overview, context }) => (
      <StepsPanel overview={overview} context={context} />
    ),
  },
  attention: {
    title: "Needs attention",
    minW: 3,
    w: 4,
    render: ({ overview, context, now, onOpenRun }) => (
      <AttentionPanel
        overview={overview}
        context={context}
        now={now}
        onOpenRun={onOpenRun}
      />
    ),
  },
}
