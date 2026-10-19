import * as React from "react"
import { defaultGenerativeUILibrary } from "@assistant-ui/generative-ui/react"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { Calendar } from "@/components/ui/calendar"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { AnswerContext } from "../answer-context"
import { replyOf } from "../actions"
import { labelled } from "../adapters"
import { isText } from "../value"

const DAY = /^(\d{4})-(\d{2})-(\d{2})$/

/** A `YYYY-MM-DD` day as a local date; none for anything else. */
function dayOf(value: unknown): Date | undefined {
  if (typeof value !== "string") return undefined
  const match = DAY.exec(value.trim())
  if (!match) return undefined
  const [year, month, date] = match.slice(1).map(Number)
  const day = new Date(year, month - 1, date)
  return day.getMonth() === month - 1 ? day : undefined
}

const two = (n: number) => String(n).padStart(2, "0")
/** A local date as `YYYY-MM-DD`, as the package's date inputs submit it. */
const isoDayOf = (day: Date) =>
  `${day.getFullYear()}-${two(day.getMonth() + 1)}-${two(day.getDate())}`
const LONG_DAY = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" })

/**
 * A day to pick, from Forge's calendar in a popover (the package draws the
 * browser's own date input, whose picker doesn't follow the theme). In a
 * form, its day goes with the others by name: a hidden input holds it, and
 * the package collects a form's controls by name. On its own, a pick sends
 * its `_action` with the day, as the package's does.
 */
function RichDatePicker({
  value,
  min,
  max,
  label,
  name,
  action,
}: {
  value?: unknown
  min?: unknown
  max?: unknown
  label?: unknown
  name?: unknown
  action?: unknown
}) {
  const answer = React.useContext(AnswerContext)
  const id = React.useId()
  const [day, setDay] = React.useState(() => dayOf(value))
  const [open, setOpen] = React.useState(false)
  const first = dayOf(min)
  const last = dayOf(max)
  const pick = (picked: Date | undefined) => {
    setDay(picked)
    setOpen(false)
    if (picked && action && typeof action === "object" && answer.live)
      answer.send(replyOf({ ...action, $input: isoDayOf(picked) }))
  }
  return (
    <div data-slot="rich-field" className="flex min-w-0 flex-col gap-1">
      {isText(label) && (
        <span
          id={`${id}-label`}
          className="text-2xs font-medium text-muted-foreground"
        >
          {label}
        </span>
      )}
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger
          render={
            <Button
              type="button"
              variant="outline"
              id={`${id}-day`}
              aria-labelledby={
                isText(label) ? `${id}-label ${id}-day` : undefined
              }
              data-slot="rich-date-picker"
              // As the form's other fields: no fill, their border.
              className="w-full justify-start border-input bg-transparent font-normal"
            />
          }
        >
          <Icon
            icon="calendar"
            data-icon="inline-start"
            className="text-muted-foreground"
          />
          {day ? (
            LONG_DAY.format(day)
          ) : (
            <span className="text-muted-foreground">Pick a date</span>
          )}
        </PopoverTrigger>
        <PopoverContent align="start" className="w-auto p-2">
          <Calendar
            mode="single"
            selected={day}
            onSelect={pick}
            defaultMonth={day ?? first}
            startMonth={first}
            endMonth={last}
            disabled={[
              ...(first ? [{ before: first }] : []),
              ...(last ? [{ after: last }] : []),
            ]}
          />
        </PopoverContent>
      </Popover>
      {isText(name) && (
        <input
          type="hidden"
          name={String(name)}
          value={day ? isoDayOf(day) : ""}
        />
      )}
    </div>
  )
}

/** The package's date input, for times and dates with times. */
const packageDatePicker = labelled(defaultGenerativeUILibrary.DatePicker)

/** A DatePicker: a day from Forge's calendar; a time as the package draws it. */
export function DatePicker(props: Record<string, unknown>) {
  return (props.inputType ?? "date") === "date" ? (
    <RichDatePicker
      value={props.value}
      min={props.min}
      max={props.max}
      label={props.label}
      name={props.name}
      action={props.$action}
    />
  ) : (
    (
      packageDatePicker.render as (
        props: Record<string, unknown>
      ) => React.ReactNode
    )(props)
  )
}
