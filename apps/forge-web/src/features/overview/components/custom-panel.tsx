import * as React from "react"

import type { ChartConfig } from "@/components/ui/chart"
import { swatch } from "@/features/overview/components/chart-kit"
import {
  BarList,
  Delta,
  Legend,
  Panel,
} from "@/features/overview/components/panels"
import { UsageChart } from "@/features/overview/components/usage-panels"
import { ChartTypeContext } from "@/features/overview/lib/chart-type"
import {
  customData,
  MEASURES,
  titleOf,
  type CustomSpec,
} from "@/features/overview/lib/custom-widget"
import { percent } from "@/features/overview/lib/format"
import {
  periodWords,
  type OrganizationOverview,
} from "@/features/overview/lib/overview"
import type { UsageContext } from "@/features/overview/lib/series"

/*
 * A widget a person defined: their measure, split and view, drawn like the
 * overview's own panels: a one-line answer (the period's figure and its
 * change), then a chart over the period, one total, or a ranking.
 */

/** How many a ranking lists before it says how many more there are. */
const RANKED = 8

type Measure = (typeof MEASURES)[keyof typeof MEASURES]

/** The figure against the period before, as a sentence's end. */
function change(
  measure: Measure,
  current: number,
  previous: number | null
): React.ReactNode {
  if (measure.rate) {
    return (
      <>
        <Delta
          current={current}
          previous={previous}
          better={measure.better}
          threshold={measure.threshold}
          format={(value) => measure.format(Math.abs(value))}
        />
        {previous !== null && " on the period before"}.
      </>
    )
  }
  if (!previous) return current > 0 ? "New this period." : null
  const relative = current / previous - 1
  if (Math.abs(relative) < 0.02) return "Steady on the period before."
  return `${relative > 0 ? "Up" : "Down"} ${Math.round(Math.abs(relative) * 100)}% on the period before.`
}

export function CustomPanel({
  overview,
  context,
  spec,
}: {
  overview: OrganizationOverview
  context: UsageContext
  spec: CustomSpec
}) {
  // One pass over the facts: cheap enough to read afresh each render.
  const data = customData(overview, context.scope, spec, context)
  const measure = MEASURES[data.spec.measure]
  const title = titleOf(data.spec)
  const words = periodWords(overview)
  const { current, previous } = data
  const nothing = current === null || (!measure.rate && current === 0)

  return (
    <Panel
      title={title}
      answer={
        nothing ? (
          `Nothing to show ${words.within}.`
        ) : (
          <>
            {measure.format(current)} {words.within}.{" "}
            {change(measure, current, previous)}
          </>
        )
      }
    >
      {data.spec.show === "time" && (
        <ChartTypeContext value={data.spec.chart}>
          <UsageChart
            config={
              Object.fromEntries(
                data.series.map((series) => [
                  series.key,
                  { label: series.label, color: series.color },
                ])
              ) satisfies ChartConfig
            }
            keys={data.series.map((series) => series.key)}
            rows={data.rows}
            unit={context.unit}
            ticks={context.ticks}
            format={measure.format}
            axis={measure.axis}
            caption={title}
            stackId="custom"
            decimals={measure.decimals}
          />
          {data.series.length > 1 && (
            <Legend
              items={data.series.map((series) => ({
                key: series.key,
                label: series.label,
                value:
                  series.current === null
                    ? "—"
                    : measure.format(series.current),
                mark: swatch(series.swatch),
              }))}
            />
          )}
        </ChartTypeContext>
      )}

      {data.spec.show === "total" && (
        <div className="border-t pt-3 pb-1">
          <p className="text-[1.75rem] font-[450] tabular-nums">
            {current === null ? "None" : measure.format(current)}
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            {previous === null || (!measure.rate && previous === 0)
              ? "Nothing in the period before."
              : `${measure.format(previous)} in the period before.`}
          </p>
        </div>
      )}

      {data.spec.show === "ranking" && data.ranking.length > 0 && (
        <>
          <BarList
            rows={data.ranking.slice(0, RANKED).map((entry) => ({
              key: entry.key,
              label: entry.label,
              value: entry.current,
              mark: swatch(entry.swatch),
              detail:
                !measure.rate && current
                  ? percent(entry.current / current)
                  : undefined,
            }))}
            format={measure.format}
          />
          {data.ranking.length > RANKED && (
            <p className="mt-2 text-2xs text-muted-foreground">
              And {data.ranking.length - RANKED} more.
            </p>
          )}
        </>
      )}
    </Panel>
  )
}
