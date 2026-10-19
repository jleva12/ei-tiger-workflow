import { cn } from "cn"
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  XAxis,
  YAxis,
} from "recharts"
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart"
import { isText } from "../value"

const axisProps = {
  tickLine: false,
  axisLine: false,
  tickMargin: 8,
  fontSize: 11,
} as const

// One series is the story (the blue ramp's step); several are compared
// subjects in the categorical set, in order, with a legend.
const ONE = "var(--viz-ramp-2)"
const SEVERAL = [
  "var(--viz-1)",
  "var(--viz-2)",
  "var(--viz-3)",
  "var(--viz-4)",
  "var(--viz-5)",
]
const SWATCH = ["bg-viz-1", "bg-viz-2", "bg-viz-3", "bg-viz-4", "bg-viz-5"]

const compact = new Intl.NumberFormat(undefined, { notation: "compact" })

type Point = { label?: unknown; value?: unknown }

/** A category's label under its bar, on two lines when it's several words. */
function WrappedTick({
  x,
  y,
  payload,
}: {
  x?: number
  y?: number
  payload?: { value?: unknown }
}) {
  const words = String(payload?.value ?? "").split(/\s+/)
  const half = Math.ceil(words.length / 2)
  const lines =
    words.length > 1
      ? [words.slice(0, half).join(" "), words.slice(half).join(" ")]
      : words
  return (
    <g transform={`translate(${x ?? 0},${y ?? 0})`}>
      <text textAnchor="middle" fontSize={11} className="fill-muted-foreground">
        {lines.map((line, index) => (
          <tspan key={index} x={0} dy={index === 0 ? 10 : 12}>
            {line}
          </tspan>
        ))}
      </text>
    </g>
  )
}

const pointsOf = (data: unknown): { label: string; value: number }[] =>
  (Array.isArray(data) ? (data as Point[]) : [])
    .map((point, index) => ({
      label: isText(point?.label) ? String(point.label) : String(index + 1),
      value: Number(point?.value),
    }))
    .filter((point) => Number.isFinite(point.value))

type ChartProps = {
  variant?: unknown
  data?: unknown
  series?: unknown
  stacked?: unknown
  showAxis?: unknown
  showLegend?: unknown
}

/** The chart's series as rows by label, `s0`, `s1`… for each series. */
function chartData({ data, series }: ChartProps) {
  const named = (Array.isArray(series) ? series : [])
    .map((each: { label?: unknown; data?: unknown }, index) => ({
      key: `s${index}`,
      label: isText(each?.label) ? String(each.label) : `Series ${index + 1}`,
      points: pointsOf(each?.data),
    }))
    .filter((each) => each.points.length)
    .slice(0, SEVERAL.length)
  const all = named.length
    ? named
    : [{ key: "s0", label: "Value", points: pointsOf(data) }]
  const rows = new Map<string, Record<string, string | number>>()
  for (const each of all) {
    for (const point of each.points) {
      const row = rows.get(point.label) ?? { label: point.label }
      row[each.key] = point.value
      rows.set(point.label, row)
    }
  }
  return { all, rows: [...rows.values()], several: named.length > 1 }
}

/**
 * A chart in Forge's colours. Labels run along the bottom and values show
 * on hover; the value axis is there when the model asks (`showAxis`), and
 * several series always have a legend. A sparkline is a small line alone.
 */
export function RichChart(props: ChartProps) {
  const { all, rows, several } = chartData(props)
  if (!rows.length) return null
  const config: ChartConfig = Object.fromEntries(
    all.map((each, index) => [
      each.key,
      { label: each.label, color: several ? SEVERAL[index] : ONE },
    ])
  )
  const sparkline = props.variant === "sparkline"
  const variant =
    props.variant === "area" ? "area" : props.variant === "bar" ? "bar" : "line"
  const stack = props.stacked === true && several ? "stack" : undefined
  const marks = all.map((each, index) => {
    const color = `var(--color-${each.key})`
    if (variant === "line")
      return (
        <Line
          key={each.key}
          dataKey={each.key}
          type="monotone"
          stroke={color}
          strokeWidth={sparkline ? 1.5 : 2}
          dot={!sparkline && rows.length <= 12}
          isAnimationActive={false}
        />
      )
    if (variant === "area")
      return (
        <Area
          key={each.key}
          dataKey={each.key}
          type="monotone"
          stroke={color}
          fill={color}
          fillOpacity={0.15}
          strokeWidth={2}
          stackId={stack}
          isAnimationActive={false}
        />
      )
    return (
      <Bar
        key={each.key}
        dataKey={each.key}
        fill={color}
        stackId={stack}
        radius={stack && index < all.length - 1 ? 0 : [2, 2, 0, 0]}
        maxBarSize={32}
        isAnimationActive={false}
      />
    )
  })
  const Chart =
    variant === "line" ? LineChart : variant === "area" ? AreaChart : BarChart
  const legend = several || (props.showLegend === true && all.length > 0)
  const categories = variant === "bar" && !sparkline && rows.length <= 8
  return (
    <figure
      data-slot="rich-chart"
      className={cn("flex min-w-0 flex-col gap-2", sparkline && "w-32")}
    >
      <ChartContainer
        config={config}
        aria-hidden="true"
        className={cn("aspect-auto w-full", sparkline ? "h-8" : "h-44")}
      >
        <Chart
          data={rows}
          margin={
            sparkline
              ? { top: 2, right: 2, left: 2, bottom: 2 }
              : { top: 6, right: 12, left: 0, bottom: 0 }
          }
        >
          {!sparkline && <CartesianGrid vertical={false} />}
          <XAxis
            dataKey="label"
            hide={sparkline}
            {...axisProps}
            // A few bars each keep their label, wrapped; a long series thins out.
            {...(categories
              ? { interval: 0, tick: <WrappedTick />, height: 44 }
              : { interval: "preserveStartEnd" as const })}
          />
          <YAxis
            hide={sparkline || props.showAxis !== true}
            width={44}
            tickCount={4}
            tickFormatter={(value: number) => compact.format(value)}
            {...axisProps}
          />
          {!sparkline && (
            <ChartTooltip
              cursor={false}
              content={<ChartTooltipContent indicator="dot" />}
            />
          )}
          {marks}
        </Chart>
      </ChartContainer>
      {legend && !sparkline && (
        <ul className="flex flex-wrap gap-x-4 gap-y-1 text-2xs text-muted-foreground">
          {all.map((each, index) => (
            <li key={each.key} className="flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className={cn(
                  "size-2.5 rounded-[2px]",
                  several ? SWATCH[index] : "bg-viz-ramp-2"
                )}
              />
              {each.label}
            </li>
          ))}
        </ul>
      )}
      {/* The chart is hidden from screen readers: its numbers as a table. */}
      <table className="sr-only">
        <thead>
          <tr>
            <th scope="col">Label</th>
            {all.map((each) => (
              <th key={each.key} scope="col">
                {each.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.label)}>
              <th scope="row">{row.label}</th>
              {all.map((each) => (
                <td key={each.key}>{row[each.key] ?? ""}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  )
}
