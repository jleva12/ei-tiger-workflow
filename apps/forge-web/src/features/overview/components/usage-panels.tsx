import * as React from "react"
import { BarChart, CartesianGrid, XAxis, YAxis } from "recharts"

import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart"
import {
  axisProps,
  chartMargin,
  SERIES_SWATCH,
  seriesConfig,
  swatch,
  tooltipRow,
} from "@/features/overview/components/chart-kit"
import {
  BarList,
  ChartTable,
  Delta,
  Facts,
  Legend,
  Panel,
  SplitSwitch,
  StackedBars,
  VolumeDelta,
  type BarListRow,
} from "@/features/overview/components/panels"
import {
  bucketLabel,
  compact,
  count,
  percent,
  plural,
  tick,
  usd,
  type Unit,
} from "@/features/overview/lib/format"
import {
  SUBJECT_KINDS,
  type BucketTotals,
} from "@/features/overview/lib/overview"
import {
  KIND_COLOR,
  KIND_ORDER,
  shortModel,
  subjectSeries,
  type UsageContext,
} from "@/features/overview/lib/series"

/*
 * The model usage panels: tokens (the overview's headline, two columns
 * wide), the models they went to, and what they cost. Workflows and agents
 * keep their colour slot on every chart (and in the ledger); types and
 * kinds of token step down the one blue ramp.
 */

const chartClass = "aspect-auto h-[172px] w-full"

/** The kinds a scope has something of, in order. */
function kindsIn(
  buckets: BucketTotals[],
  measure: "tokens" | "cost" | "started"
) {
  return KIND_ORDER.filter((kind) =>
    buckets.some((bucket) => bucket.byKind[kind][measure] > 0)
  )
}

/** A period's stacked bars, with their table twin for screen readers. */
export function UsageChart({
  config,
  keys,
  rows,
  unit,
  ticks,
  format,
  axis,
  caption,
  stackId,
  decimals = false,
}: {
  config: ChartConfig
  keys: string[]
  rows: Record<string, number>[]
  unit: Unit
  ticks: number[]
  format: (value: number) => string
  axis: (value: number) => string
  caption: string
  stackId: string
  /** Ticks between whole numbers (dollars); counts stay whole. */
  decimals?: boolean
}) {
  return (
    <>
      <ChartContainer config={config} aria-hidden="true" className={chartClass}>
        <BarChart data={rows} margin={chartMargin}>
          <CartesianGrid vertical={false} />
          <XAxis
            dataKey="start"
            ticks={ticks}
            interval={0}
            tickFormatter={tick}
            {...axisProps}
          />
          <YAxis
            width={44}
            tickCount={4}
            allowDecimals={decimals}
            tickFormatter={axis}
            {...axisProps}
          />
          <ChartTooltip
            cursor={false}
            content={
              <ChartTooltipContent
                labelFormatter={(_, payload) =>
                  bucketLabel(
                    (payload?.[0]?.payload as { start?: number })?.start,
                    unit
                  )
                }
                formatter={tooltipRow(config, format)}
              />
            }
          />
          {StackedBars({ keys, stackId })}
        </BarChart>
      </ChartContainer>
      <ChartTable
        caption={caption}
        columns={[
          unit === "week" ? "Week" : "Day",
          ...keys.map((key) => String(config[key]?.label ?? key)),
        ]}
        rows={rows.map((row) => [
          bucketLabel(row.start, unit),
          ...keys.map((key) => format(row[key] ?? 0)),
        ])}
      />
    </>
  )
}

/* Tokens ------------------------------------------------------------------ */

type TokenSplit = "type" | "subject" | "token"

const TOKEN_KINDS = {
  output: {
    label: "Output",
    color: "var(--viz-ramp-1)",
    swatch: "bg-viz-ramp-1",
  },
  fresh: {
    label: "New input",
    color: "var(--viz-ramp-2)",
    swatch: "bg-viz-ramp-2",
  },
  cached: {
    label: "Cached input",
    color: "var(--viz-ramp-4)",
    swatch: "bg-viz-ramp-4",
  },
} as const

const tokenKindConfig = Object.fromEntries(
  Object.entries(TOKEN_KINDS).map(([key, kind]) => [
    key,
    { label: kind.label, color: kind.color },
  ])
) satisfies ChartConfig

/**
 * The period's tokens, the overview's headline: stacked by type, by
 * workflow or agent (in their slots), or by kind of token.
 */
export function TokensPanel({ context }: { context: UsageContext }) {
  const { summary, scope, unit, ticks } = context
  const { current, previous } = summary
  const options: Partial<Record<TokenSplit, string>> =
    scope.level === "organization"
      ? { type: "By type", subject: "By workflow", token: "By token" }
      : scope.level === "kind"
        ? { subject: "By name", token: "By token" }
        : { token: "By token" }
  const fallback: TokenSplit =
    scope.level === "organization"
      ? "type"
      : scope.level === "kind"
        ? "subject"
        : "token"
  const [chosen, setChosen] = React.useState<TokenSplit>(fallback)
  const split: TokenSplit = chosen in options ? chosen : fallback

  const cachedShare = current.input ? current.cached / current.input : 0
  const perCall = current.calls ? current.tokens / current.calls : null
  const runs = current.started
  const perRun = runs ? current.tokens / runs : null

  let config: ChartConfig
  let keys: string[]
  let rows: Record<string, number>[]
  let legend: {
    key: string
    label: string
    value: string
    mark: React.ReactNode
  }[]
  if (split === "token") {
    config = tokenKindConfig
    keys = ["output", "fresh", "cached"]
    rows = summary.buckets.map((bucket) => ({
      start: bucket.start,
      output: bucket.output,
      fresh: Math.max(0, bucket.input - bucket.cached),
      cached: bucket.cached,
    }))
    legend = [
      {
        key: "output",
        label: "Output",
        value: compact(current.output),
        mark: swatch(TOKEN_KINDS.output.swatch),
      },
      {
        key: "fresh",
        label: "New input",
        value: compact(Math.max(0, current.input - current.cached)),
        mark: swatch(TOKEN_KINDS.fresh.swatch),
      },
      {
        key: "cached",
        label: "Cached input",
        value: compact(current.cached),
        mark: swatch(TOKEN_KINDS.cached.swatch),
      },
    ]
  } else if (split === "type") {
    const kinds = kindsIn(summary.buckets, "tokens")
    config = Object.fromEntries(
      kinds.map((kind) => [
        kind,
        { label: SUBJECT_KINDS[kind].plural, color: KIND_COLOR[kind].color },
      ])
    )
    keys = kinds
    rows = summary.buckets.map((bucket) => ({
      start: bucket.start,
      ...Object.fromEntries(
        kinds.map((kind) => [kind, bucket.byKind[kind].tokens])
      ),
    }))
    legend = kinds.map((kind) => ({
      key: kind,
      label: SUBJECT_KINDS[kind].plural,
      value: compact(
        summary.buckets.reduce(
          (sum, bucket) => sum + bucket.byKind[kind].tokens,
          0
        )
      ),
      mark: swatch(KIND_COLOR[kind].swatch),
    }))
  } else {
    const { series, rows: stacked } = subjectSeries(context, "tokens")
    config = seriesConfig(series)
    keys = series.map((entry) => entry.key)
    rows = stacked
    legend = series.map((entry) => ({
      key: entry.key,
      label: entry.label,
      value: compact(
        stacked.reduce((sum, row) => sum + (row[entry.key] ?? 0), 0)
      ),
      mark: swatch(SERIES_SWATCH[entry.key]),
    }))
  }

  return (
    <Panel
      title="Model tokens"
      className="col-span-2 @max-[900px]/shell:col-span-1"
      action={
        Object.keys(options).length > 1 && (
          <SplitSwitch
            label="Split tokens"
            options={options as Record<TokenSplit, string>}
            value={split}
            onValueChange={setChosen}
          />
        )
      }
      answer={
        current.tokens === 0 ? (
          "No model tokens in this period."
        ) : (
          <>
            <strong className="font-medium text-foreground">
              {compact(current.tokens)} tokens
            </strong>{" "}
            over {plural(current.calls, "model call")}, {percent(cachedShare)}{" "}
            of the input read back from cache
            {perRun !== null && <>, {compact(perRun)} a run</>}.
            {previous.tokens > 0 && (
              <>
                {" "}
                <VolumeDelta
                  current={current.tokens}
                  previous={previous.tokens}
                />{" "}
                on the period before.
              </>
            )}
          </>
        )
      }
    >
      <UsageChart
        config={config}
        keys={keys}
        rows={rows}
        unit={unit}
        ticks={ticks}
        format={count}
        axis={compact}
        caption={`Model tokens ${split === "token" ? "by kind" : split === "type" ? "by type" : "by workflow or agent"}`}
        stackId="tokens"
      />
      <Legend items={legend} />
      <Facts
        className="grid grid-cols-2 gap-x-6 border-t-0 @max-[600px]/shell:grid-cols-1 [&>div]:border-b"
        items={[
          ["Input", count(current.input)],
          ["Output", count(current.output)],
          ["Cached input", count(current.cached)],
          ["Thinking (in output)", count(current.thinking)],
          [
            "Tokens per model call",
            perCall === null ? "None" : compact(perCall),
          ],
          [
            "Model calls",
            <>
              {count(current.calls)}
              {current.failedCalls > 0 && (
                <span className="ml-1.5 font-normal text-danger-foreground">
                  {count(current.failedCalls)} failed
                </span>
              )}
            </>,
          ],
        ]}
      />
    </Panel>
  )
}

/* Models ------------------------------------------------------------------ */

/** Which models the tokens and dollars went to. */
export function ModelsPanel({ context }: { context: UsageContext }) {
  const { summary } = context
  const { models, current } = summary
  const top = models[0]
  return (
    <Panel
      title="Models"
      answer={
        !top || current.tokens === 0
          ? "No model was called in this period."
          : `${shortModel(top.model)} took ${percent(top.tokens / current.tokens)} of the tokens${
              current.cost > 0
                ? ` and ${percent(top.cost / current.cost)} of the spend.`
                : "."
            }`
      }
    >
      {models.length > 0 && (
        <table className="w-full border-t text-xs tabular-nums">
          <thead>
            <tr className="border-b text-2xs text-muted-foreground">
              <th scope="col" className="py-1.5 text-left font-normal">
                Model
              </th>
              <th scope="col" className="py-1.5 text-right font-normal">
                Calls
              </th>
              <th scope="col" className="py-1.5 pl-3 text-right font-normal">
                Tokens
              </th>
              <th scope="col" className="py-1.5 pl-3 text-right font-normal">
                Spend
              </th>
            </tr>
          </thead>
          <tbody>
            {models.slice(0, 8).map((model) => (
              <tr key={model.model} className="border-b last:border-b-0">
                <th
                  scope="row"
                  className="max-w-0 py-2 text-left font-normal text-muted-foreground"
                >
                  <span className="block truncate" title={model.model}>
                    {shortModel(model.model)}
                  </span>
                </th>
                <td className="py-2 text-right">{compact(model.calls)}</td>
                <td className="py-2 pl-3 text-right font-medium">
                  {compact(model.tokens)}
                </td>
                <td className="py-2 pl-3 text-right font-medium">
                  {model.cost > 0 || model.unpriced === 0
                    ? usd(model.cost)
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <Facts
        items={[
          [
            "Cache hits",
            current.input ? percent(current.cached / current.input) : "None",
          ],
          [
            "Thinking share of output",
            current.output
              ? percent(current.thinking / current.output)
              : "None",
          ],
          [
            "Failed model calls",
            current.calls
              ? percent(current.failedCalls / current.calls)
              : "None",
          ],
        ]}
      />
    </Panel>
  )
}

/* Spend ------------------------------------------------------------------- */

const SPEND_ROWS = 6

/** Model spend over the period, by workflow or agent, and who drives it. */
export function SpendPanel({ context }: { context: UsageContext }) {
  const { summary, unit, ticks, slots, subjects } = context
  const { current, previous } = summary
  const { series, rows } = subjectSeries(context, "cost")
  const stacked = series.length > 0
  const config = seriesConfig(
    stacked ? series : [{ key: "m1", label: "Spend" }]
  )
  const keys = stacked ? series.map((entry) => entry.key) : ["m1"]
  const data = stacked
    ? rows
    : summary.buckets.map((bucket) => ({
        start: bucket.start,
        m1: bucket.cost,
      }))
  const perRun = current.started ? current.cost / current.started : null

  // Who spent in the period, most first; past a few, the rest together.
  const spenders = new Map<string, number>()
  for (const bucket of summary.buckets) {
    for (const [key, value] of Object.entries(bucket.bySubject)) {
      spenders.set(key, (spenders.get(key) ?? 0) + value.cost)
    }
  }
  const ranked = [...spenders.entries()]
    .filter(([, cost]) => cost > 0)
    .sort((a, b) => b[1] - a[1])
  const shown =
    ranked.length > SPEND_ROWS ? ranked.slice(0, SPEND_ROWS - 1) : ranked
  const rest = ranked.slice(shown.length)
  const list: BarListRow[] = shown.map(([key, cost]) => {
    const slot = slots.get(key) ?? "other"
    return {
      key,
      label: subjects.get(key)?.name ?? key,
      value: cost,
      detail: current.cost ? percent(cost / current.cost) : undefined,
      mark: swatch(SERIES_SWATCH[slot]),
      barClassName: SERIES_SWATCH[slot],
    }
  })
  if (rest.length) {
    list.push({
      key: "rest",
      label: plural(rest.length, "other"),
      value: rest.reduce((sum, [, cost]) => sum + cost, 0),
      mark: swatch(SERIES_SWATCH.other),
      barClassName: SERIES_SWATCH.other,
    })
  }

  return (
    <Panel
      title="Model spend"
      answer={
        current.cost === 0 ? (
          current.unpriced > 0 ? (
            `No priced model calls: ${plural(current.unpriced, "call")} ran on models without a price.`
          ) : (
            "No model spend in this period."
          )
        ) : (
          <>
            {usd(current.cost)} in all
            {perRun !== null && <>, {usd(perRun)} a run</>}.
            {previous.cost > 0 && (
              <>
                {" "}
                <Delta
                  current={current.cost}
                  previous={previous.cost}
                  better="lower"
                  threshold={Math.max(0.01, current.cost * 0.05)}
                  format={(change) => usd(Math.abs(change))}
                />{" "}
                on the period before.
              </>
            )}
          </>
        )
      }
    >
      <UsageChart
        config={config}
        keys={keys}
        rows={data}
        unit={unit}
        ticks={ticks}
        format={usd}
        axis={(value) =>
          value < 10 && value % 1
            ? `$${value.toFixed(2)}`
            : `$${compact(value)}`
        }
        caption="Model spend by workflow or agent"
        stackId="spend"
        decimals
      />
      {list.length > 0 && <BarList className="mt-3" rows={list} format={usd} />}
      {current.unpriced > 0 && (
        <p className="mt-2 text-2xs text-muted-foreground">
          {plural(current.unpriced, "model call")} ran on models without a price
          and {current.unpriced === 1 ? "isn't" : "aren't"} counted.
        </p>
      )}
    </Panel>
  )
}
