import * as React from "react"

import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command"
import { KindGlyph } from "./glyph"
import { useBuilder, useBuilderApi, type PendingConnection } from "./store"
import { useBuilderUi } from "./ui"

// Words, not fuzzy letters: a step whose name starts with the search, then
// one whose name holds it, then one whose keywords do.
const byWords = (value: string, search: string, keywords?: string[]) => {
  const words = search.trim().toLowerCase()
  if (!words) return 1
  const label = value.toLowerCase()
  if (label.startsWith(words)) return 1
  if (label.includes(words)) return 0.8
  return keywords?.some((k) => k.includes(words)) ? 0.5 : 0
}

const WIDTH = 272
const HEIGHT = 320

/**
 * Where a branch dragged onto empty canvas lands: a searchable list of
 * steps, opened at the pointer. The step picked goes there, connected to
 * that branch. Escape, a click away or picking nothing closes it.
 */
export function StepPicker({ pending }: { pending: PendingConnection }) {
  const api = useBuilderApi()
  const adapter = useBuilder((s) => s.adapter)
  const { nouns } = useBuilderUi()
  const from = useBuilder((s) => s.nodes.find((n) => n.id === pending.source))
  const panel = React.useRef<HTMLDivElement>(null)
  const [bounds, setBounds] = React.useState<{ width: number; height: number }>()

  React.useLayoutEffect(() => {
    const parent = panel.current?.parentElement
    if (parent) setBounds({ width: parent.clientWidth, height: parent.clientHeight })
  }, [])

  React.useEffect(() => {
    const close = (event: PointerEvent) => {
      if (!panel.current?.contains(event.target as Node)) api.getState().setPending(null)
    }
    document.addEventListener("pointerdown", close, true)
    return () => document.removeEventListener("pointerdown", close, true)
  }, [api])

  // Beside the pointer, away from the step the branch came from (to its
  // left), and always inside the canvas.
  const width = bounds?.width ?? Infinity
  const height = bounds?.height ?? Infinity
  const right = pending.screen.x + 12
  const left = Math.max(
    12,
    right + WIDTH + 12 <= width ? right : Math.min(pending.screen.x - WIDTH - 12, width - WIDTH - 12)
  )
  const top = Math.max(12, Math.min(pending.screen.y - 40, height - HEIGHT - 12))
  // Only kinds with a way in, and that the way out takes, can follow it.
  const kinds = adapter.kindList.filter(
    (kind) =>
      adapter.hasInput(kind) &&
      (!from || (adapter.accepts?.(from.data, pending.output, kind) ?? true))
  )
  const pick = (kind: string) => {
    const store = api.getState()
    store.addStep(kind, pending.at, { source: pending.source, output: pending.output })
    store.setPending(null)
  }

  return (
    <div
      ref={panel}
      role="dialog"
      aria-label={`Add a ${nouns.step}`}
      className="absolute z-10 flex flex-col overflow-hidden rounded-(--radius-band) border bg-background shadow-(--shadow-float)"
      style={{ left, top, width: WIDTH, maxHeight: HEIGHT }}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation()
          api.getState().setPending(null)
        }
      }}
    >
      <p className="border-b px-3 py-2 text-2xs text-muted-foreground">
        After <span className="font-medium text-foreground">{from?.data.name}</span>
      </p>
      <Command filter={byWords} className="min-h-0 flex-1 rounded-none">
        <CommandInput placeholder={`Find a ${nouns.step}`} autoFocus />
        <CommandList className="max-h-none min-h-0 flex-1 overflow-y-auto">
          <CommandEmpty>No {nouns.step} by that name.</CommandEmpty>
          {adapter.groups.map((group) => {
            const members = kinds.filter((kind) => adapter.kinds[kind].group === group.id)
            if (!members.length) return null
            return (
              <CommandGroup key={group.id} heading={group.label}>
                {members.map((kind) => (
                  <CommandItem
                    key={kind}
                    value={adapter.kinds[kind].label}
                    keywords={adapter.kinds[kind].keywords.split(" ")}
                    onSelect={() => pick(kind)}
                  >
                    <KindGlyph info={adapter.kinds[kind]} size="sm" />
                    {adapter.kinds[kind].label}
                  </CommandItem>
                ))}
              </CommandGroup>
            )
          })}
        </CommandList>
      </Command>
    </div>
  )
}
