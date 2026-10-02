import type { ToolCallMessagePartComponent } from "@assistant-ui/react"

import { toolResult } from "../lib/tool-result"

import { ToolFrame } from "./tool-frame"

type CalculateArgs = { expression: string }
type CalculateResult = { expression: string; result: number; error: string }

export const CalculateUI: ToolCallMessagePartComponent<CalculateArgs> = (
  part
) => {
  const { result, error } = toolResult<CalculateResult>(part.result)
  return (
    <ToolFrame
      part={part}
      running={<>Calculating {part.args.expression}</>}
      label={
        error ? (
          <>Couldn't calculate {part.args.expression}</>
        ) : (
          <>
            Calculated{" "}
            <b className="font-mono">
              {part.args.expression} = {String(result)}
            </b>
          </>
        )
      }
    />
  )
}

type DiceArgs = { sides?: number; count?: number }
type DiceResult = { rolls: number[]; total: number; error: string }

export const RollDiceUI: ToolCallMessagePartComponent<DiceArgs> = (part) => {
  const { rolls = [], total, error } = toolResult<DiceResult>(part.result)
  const sides = part.args.sides ?? 6
  return (
    <ToolFrame
      part={part}
      running={<>Rolling dice</>}
      label={
        error ? (
          <>Couldn't roll: {error}</>
        ) : (
          <>
            Rolled {rolls.length}d{sides}: <b>{total}</b>
          </>
        )
      }
    >
      {rolls.length > 0 && (
        <div className="flex flex-wrap gap-1.5 ps-6">
          {rolls.map((roll, index) => (
            <span
              key={index}
              className="flex size-8 animate-in items-center justify-center rounded-md border bg-card font-mono text-sm text-foreground tabular-nums shadow-xs duration-300 fill-mode-both zoom-in-50"
              style={{ animationDelay: `${index * 40}ms` }}
            >
              {roll}
            </span>
          ))}
        </div>
      )}
    </ToolFrame>
  )
}

type TimeArgs = { timezone?: string }
type TimeResult = {
  timezone: string
  iso: string
  weekday: string
  error: string
}

export const CurrentTimeUI: ToolCallMessagePartComponent<TimeArgs> = (part) => {
  const { iso, timezone, error } = toolResult<TimeResult>(part.result)
  const zone = timezone ?? part.args.timezone ?? "UTC"
  // The time as it was in that zone when the tool ran.
  const clock = iso ? iso.slice(11, 16) : undefined
  const date = iso
    ? new Date(iso.slice(0, 10) + "T12:00:00Z").toLocaleDateString(undefined, {
        weekday: "long",
        month: "long",
        day: "numeric",
        timeZone: "UTC",
      })
    : undefined
  return (
    <ToolFrame
      part={part}
      running={<>Checking the time in {zone}</>}
      label={error ? <>{error}</> : <>Checked the time in {zone}</>}
    >
      {clock && (
        <div className="ms-6 flex w-fit items-baseline gap-3 rounded-md border bg-card px-3 py-2 shadow-xs">
          <span className="font-mono text-2xl text-foreground tabular-nums">
            {clock}
          </span>
          <span className="text-xs text-muted-foreground">
            {date} · {zone}
          </span>
        </div>
      )}
    </ToolFrame>
  )
}
