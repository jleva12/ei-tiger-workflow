import * as React from "react"
import {
  defaultGenerativeUILibrary,
  renderGenerativeUI,
  type GenerativeUIAction,
  type GenerativeUILibrary,
} from "@assistant-ui/generative-ui/react"
import {
  useAui,
  useAuiState,
  type ToolCallMessagePartProps,
} from "@assistant-ui/react"
import { cn } from "cn"
import ReactMarkdown from "react-markdown"
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
import remarkGfm from "remark-gfm"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

import "./rich-answers.css"
import type { MapPlace } from "./rich-map"
import {
  choicesOf,
  RICH_ANSWER_COMPONENTS,
  RICH_ANSWER_VOCABULARY,
  replyOf,
  shownOf,
  withQuietFields,
  type MessagePart,
  type RichAnswerComponent,
} from "./rich-answers-schema"

/*
 * Rich answers: an agent's `present` call drawn in the chat with
 * assistant-ui's Generative UI. The call is the agent server's (a backend
 * tool answering `{"shown": true}`), so this only draws its arguments, once
 * the server has taken them; a call it refused (`{"shown": false}`) isn't
 * drawn: the model is told why and sends it again.
 *
 * Most components are the package's own renderers (semantic HTML with
 * `data-aui` hooks), styled by `rich-answers.css` with the theme's tokens.
 * Forge draws Fact, Table, Chart, Steps and Choices with its own pieces,
 * Markdown with the chat's markdown, and Diagram and Map with Mermaid and
 * Leaflet, loaded when an answer has one. The model's props aren't checked
 * before they're drawn, so each piece takes what fits.
 *
 * What the person does comes back to the agent as their next message, the
 * way a follow-up chip's question does: a Choices option's reply, or a
 * control's `_action` (its text, then what they chose or entered; a form's
 * values by name, `replyOf`). A composition acts only while it's the
 * conversation's last reply, nothing runs and the chat can send; after
 * that its controls are off, and a Choices pick (the next message's text)
 * stays marked, so a reloaded conversation shows the same.
 */

const isText = (value: unknown): value is string | number =>
  typeof value === "string" ||
  (typeof value === "number" && Number.isFinite(value))

/** What a composition may do: act, while it's live; what its Choices picked. */
const AnswerContext = React.createContext<{
  live: boolean
  picked?: string
  send: (text: string) => void
}>({ live: false, send: () => {} })

/* Facts -------------------------------------------------------------------- */

const ARROWS = { up: "↑", down: "↓", flat: "→" } as const

function RichFact({
  label,
  value,
  delta,
  trend,
  upIsGood,
}: {
  label?: unknown
  value?: unknown
  delta?: unknown
  trend?: unknown
  upIsGood?: unknown
}) {
  const moved = trend === "up" || trend === "down"
  const better = moved && (trend === "up") === (upIsGood !== false)
  const arrow =
    trend === "up" || trend === "down" || trend === "flat"
      ? ARROWS[trend]
      : undefined
  return (
    <dl
      data-slot="rich-fact"
      className="flex min-w-0 flex-col gap-1 rounded-(--radius-item) bg-muted/60 px-3 py-2.5"
    >
      <dt className="truncate text-2xs text-muted-foreground">
        {isText(label) ? label : ""}
      </dt>
      <dd className="text-[1.0625rem]/[1.3] font-[550] tracking-[-.2px] text-foreground tabular-nums">
        {isText(value) ? value : "—"}
      </dd>
      {(typeof delta === "string" && delta) || arrow ? (
        <dd
          className={cn(
            "text-2xs tabular-nums",
            !moved
              ? "text-muted-foreground"
              : better
                ? "text-success-foreground"
                : "text-danger-foreground"
          )}
        >
          {arrow && <span aria-hidden="true">{arrow} </span>}
          {typeof delta === "string" ? delta : ""}
          {moved && (
            <span className="sr-only">{better ? ", better" : ", worse"}</span>
          )}
        </dd>
      ) : null}
    </dl>
  )
}

/* Tables ------------------------------------------------------------------- */

type Format = { kind?: unknown; currency?: unknown; decimals?: unknown }
type Column = { label: string; align?: unknown; format?: Format }
type Cell = string | number | boolean

function formatCell(value: unknown, format: Format | undefined): string {
  if (typeof value === "boolean") return value ? "Yes" : "No"
  if (!isText(value)) return ""
  if (!format) return typeof value === "number" ? value.toLocaleString() : value
  const decimals =
    typeof format.decimals === "number" &&
    format.decimals >= 0 &&
    format.decimals <= 6
      ? format.decimals
      : undefined
  try {
    if (format.kind === "date") {
      const date = new Date(value)
      return Number.isNaN(date.getTime())
        ? String(value)
        : date.toLocaleDateString()
    }
    const number = typeof value === "number" ? value : Number(value)
    if (!Number.isFinite(number)) return String(value)
    return new Intl.NumberFormat(undefined, {
      ...(format.kind === "currency" && {
        style: "currency",
        currency: typeof format.currency === "string" ? format.currency : "USD",
      }),
      ...(format.kind === "percent" && { style: "percent" }),
      ...(decimals !== undefined && {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      }),
    }).format(number)
  } catch {
    // A currency code Intl doesn't know.
    return String(value)
  }
}

const compareCells = (a: Cell | undefined, b: Cell | undefined) =>
  typeof a === "number" && typeof b === "number"
    ? a - b
    : String(a ?? "").localeCompare(String(b ?? ""), undefined, {
        numeric: true,
      })

function RichTable({
  columns,
  rows,
  sortable,
}: {
  columns?: unknown
  rows?: unknown
  sortable?: unknown
}) {
  const [sort, setSort] = React.useState<{ index: number; up: boolean }>()
  const heads = (Array.isArray(columns) ? columns : []).filter(
    (column): column is Column =>
      typeof column === "object" && column !== null && isText(column.label)
  )
  const body = (Array.isArray(rows) ? rows : []).filter((row): row is Cell[] =>
    Array.isArray(row)
  )
  const width = Math.max(heads.length, ...body.map((row) => row.length))
  if (!width) return null
  const sorted = sort
    ? [...body].sort(
        (a, b) =>
          compareCells(a[sort.index], b[sort.index]) * (sort.up ? 1 : -1)
      )
    : body
  const end = (index: number) => heads[index]?.align === "end"
  const canSort = sortable === true
  return (
    <Table data-slot="rich-table" className="text-xs">
      {heads.length > 0 && (
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            {Array.from({ length: width }, (_, index) => {
              const label = heads[index]?.label ?? ""
              const by = sort?.index === index
              return (
                <TableHead
                  key={index}
                  aria-sort={
                    by ? (sort.up ? "ascending" : "descending") : undefined
                  }
                  className={cn(
                    "h-8 px-2 text-2xs font-medium text-muted-foreground first:pl-0 last:pr-0",
                    end(index) && "text-right"
                  )}
                >
                  {canSort ? (
                    <button
                      type="button"
                      className="inline-flex items-center gap-1 hover:text-foreground"
                      onClick={() =>
                        setSort({ index, up: by ? !sort.up : true })
                      }
                    >
                      {label}
                      {by && (
                        <Icon
                          icon={sort.up ? "up" : "down"}
                          className="size-3"
                        />
                      )}
                    </button>
                  ) : (
                    label
                  )}
                </TableHead>
              )
            })}
          </TableRow>
        </TableHeader>
      )}
      <TableBody>
        {sorted.map((row, rowIndex) => (
          <TableRow key={rowIndex} className="hover:bg-transparent">
            {Array.from({ length: width }, (_, index) => (
              <TableCell
                key={index}
                className={cn(
                  "px-2 py-1.5 tabular-nums first:pl-0 last:pr-0",
                  end(index) && "text-right"
                )}
              >
                {formatCell(row[index], heads[index]?.format)}
              </TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

/* Charts ------------------------------------------------------------------- */

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
function RichChart(props: ChartProps) {
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

/* Steps -------------------------------------------------------------------- */

function RichSteps({ steps }: { steps?: unknown }) {
  const items = (Array.isArray(steps) ? steps : []).filter(
    (step): step is { title: string | number; detail?: unknown } =>
      isText(step?.title)
  )
  if (!items.length) return null
  return (
    <ol data-slot="rich-steps" className="flex flex-col gap-2.5">
      {items.map((step, index) => (
        <li key={index} className="flex gap-2.5">
          <span
            aria-hidden="true"
            className="mt-px flex size-5 shrink-0 items-center justify-center rounded-full bg-muted text-3xs font-medium text-muted-foreground tabular-nums"
          >
            {index + 1}
          </span>
          <div className="flex min-w-0 flex-col gap-0.5 pt-0.5">
            <p className="font-medium text-foreground">{step.title}</p>
            {isText(step.detail) && (
              <p className="text-muted-foreground">{step.detail}</p>
            )}
          </div>
        </li>
      ))}
    </ol>
  )
}

/* Choices ------------------------------------------------------------------ */

function RichChoices({ options }: { options?: unknown }) {
  const { live, picked, send } = React.useContext(AnswerContext)
  const choices = choicesOf(options)
  if (!choices.length) return null
  return (
    <div
      data-slot="rich-choices"
      role="group"
      aria-label="Choices"
      className="flex flex-wrap gap-1.5"
    >
      {choices.map((choice, index) => {
        const chosen = picked === choice.reply
        return (
          <Button
            key={index}
            type="button"
            size="sm"
            variant={chosen ? "secondary" : "outline"}
            disabled={!live}
            aria-pressed={picked === undefined ? undefined : chosen}
            title={choice.reply === choice.label ? undefined : choice.reply}
            onClick={() => send(choice.reply)}
            className={cn(
              "h-auto min-h-7 py-1 text-start whitespace-normal",
              chosen && "disabled:opacity-100"
            )}
          >
            {chosen && <Icon icon="check" data-icon="inline-start" />}
            {choice.label}
          </Button>
        )
      })}
    </div>
  )
}

/* Markdown, diagrams and maps ---------------------------------------------- */

function RichMarkdown({
  value,
  children,
}: {
  value?: unknown
  children?: React.ReactNode
}) {
  return (
    <div data-aui="markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>
        {typeof value === "string" ? value : ""}
      </ReactMarkdown>
      {children}
    </div>
  )
}

const LazyDiagram = React.lazy(() => import("./rich-diagram"))
const LazyMap = React.lazy(() => import("./rich-map"))

function RichDiagram({ code, title }: { code?: unknown; title?: unknown }) {
  if (typeof code !== "string" || !code.trim()) return null
  return (
    <React.Suspense fallback={<Skeleton className="h-32 w-full" />}>
      <LazyDiagram
        code={code}
        title={isText(title) ? String(title) : undefined}
      />
    </React.Suspense>
  )
}

const placesOf = (places: unknown): MapPlace[] =>
  (Array.isArray(places) ? places : []).flatMap((place: unknown) => {
    if (typeof place !== "object" || place === null) return []
    const { label, lat, lng, detail } = place as Record<string, unknown>
    return typeof label === "string" &&
      typeof lat === "number" &&
      typeof lng === "number" &&
      Math.abs(lat) <= 90 &&
      Math.abs(lng) <= 180
      ? [
          {
            label,
            lat,
            lng,
            detail: typeof detail === "string" ? detail : undefined,
          },
        ]
      : []
  })

function RichMap({ places, route }: { places?: unknown; route?: unknown }) {
  const pins = placesOf(places)
  if (!pins.length) return null
  return (
    <React.Suspense fallback={<Skeleton className="h-56 w-full" />}>
      <LazyMap places={pins} route={route === true} />
    </React.Suspense>
  )
}

/* The library -------------------------------------------------------------- */

/** The components Forge draws itself; the package draws the rest. */
const FORGE: Partial<
  Record<RichAnswerComponent, React.ComponentType<Record<string, unknown>>>
> = {
  Fact: RichFact,
  Table: RichTable,
  Chart: RichChart,
  Steps: RichSteps,
  Choices: RichChoices,
  Markdown: RichMarkdown,
  Diagram: RichDiagram,
  Map: RichMap,
}

/**
 * The package's fields that name themselves only to screen readers (their
 * `label` is an `aria-label`): Forge shows the label over them too.
 */
const LABELLED: readonly RichAnswerComponent[] = [
  "Input",
  "Select",
  "DatePicker",
  "RadioGroup",
  "CheckboxGroup",
]

type Component = GenerativeUILibrary[string]

function labelled(own: Component): Component {
  const render = own.render as (
    props: Record<string, unknown>
  ) => React.ReactNode
  return {
    ...own,
    render: (props: Record<string, unknown>) => {
      const control = render(props)
      if (!isText(props.label)) return control
      return (
        <div data-slot="rich-field" className="flex min-w-0 flex-col gap-1">
          {/* The control names itself; this is for the eye. */}
          <span
            aria-hidden="true"
            className="text-2xs font-medium text-muted-foreground"
          >
            {props.label}
          </span>
          {control}
        </div>
      )
    },
  } as Component
}

/** Every component the chat draws, whichever an agent picked. */
const LIBRARY = Object.fromEntries(
  RICH_ANSWER_COMPONENTS.map((name) => {
    const Draw = FORGE[name]
    const own = defaultGenerativeUILibrary[name]
    if (!Draw && own)
      return [name, LABELLED.includes(name) ? labelled(own) : own]
    return [
      name,
      {
        ...RICH_ANSWER_VOCABULARY[name],
        streamProperties: own?.streamProperties,
        // The package's own keys ($status, $dispatch…) stay out of Forge's pieces.
        render: (props: Record<string, unknown>) =>
          Draw ? (
            <Draw
              {...Object.fromEntries(
                Object.entries(props).filter(([key]) => !key.startsWith("$"))
              )}
            />
          ) : null,
      },
    ]
  })
) as GenerativeUILibrary

/* The tool ----------------------------------------------------------------- */

/** The text of the person's message right after this one; none when there's none yet. */
function nextUserText(s: {
  message: { id: string }
  thread: {
    messages: readonly {
      id: string
      role: string
      content: readonly MessagePart[]
    }[]
  }
}) {
  const messages = s.thread.messages
  const next = messages[messages.findIndex((m) => m.id === s.message.id) + 1]
  if (next?.role !== "user") return undefined
  return next.content
    .flatMap((part) => (part.type === "text" && part.text ? [part.text] : []))
    .join("\n")
    .trim()
}

/**
 * What one call's composition may do: act while its message is the
 * conversation's last, nothing runs and the chat can send; what it sends
 * goes as the person's message, with what the composer sends beside it.
 */
function useAnswer() {
  const aui = useAui()
  const [sent, setSent] = React.useState(false)
  const live = useAuiState(
    (s) => s.message.isLast && !s.thread.isRunning && !s.thread.isDisabled
  )
  const picked = useAuiState(nextUserText)
  const send = React.useCallback(
    (text: string) => {
      if (!text || aui.thread.getState().isRunning) return
      setSent(true)
      aui.thread.append({
        content: [{ type: "text", text }],
        runConfig: aui.composer.getState().runConfig,
      })
    },
    [aui]
  )
  return React.useMemo(
    () => ({ live: live && !sent, picked, send }),
    [live, sent, picked, send]
  )
}

/**
 * A `present` call in the conversation: the composition its arguments
 * describe, once the agent server has taken it. Its controls work while
 * it's live; otherwise they're kept, off.
 */
export function PresentUI(props: ToolCallMessagePartProps) {
  const answer = useAnswer()
  const shown = shownOf(props.result)
  // Drawn once the server took it: a refused call is sent again, fixed, and
  // only that one shows. One with no verdict at all is drawn once it's done.
  if (
    shown === false ||
    (shown === undefined && props.status.type === "running")
  )
    return null
  const dispatch = (action: GenerativeUIAction) => answer.send(replyOf(action))
  const drawn = renderGenerativeUI(withQuietFields(props.args), LIBRARY, {
    // The server has its whole arguments by now.
    status: "done",
    ...(answer.live && { dispatch }),
  })
  return (
    <AnswerContext.Provider value={answer}>
      <div data-slot="rich-answer" className="my-2 min-w-0">
        <div data-aui="root">
          {answer.live ? (
            drawn
          ) : (
            <fieldset disabled data-aui="answered">
              {drawn}
            </fieldset>
          )}
        </div>
      </div>
    </AnswerContext.Provider>
  )
}
