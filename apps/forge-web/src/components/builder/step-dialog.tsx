import * as React from "react"
import { Dialog as DialogPrimitive } from "@base-ui/react/dialog"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogDescription,
  DialogOverlay,
  DialogPortal,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { FieldIssuesProvider } from "./fields/field-issues"
import { KindGlyph } from "./glyph"
import { IssueList } from "./issue-list"
import { useBuilder, useBuilderApi, type FlowNode } from "./store"
import { useBuilderUi } from "./ui"
import {
  CompanionContext,
  DIALOG_CARD,
  reducedMotion,
  withViewTransition,
  type Companion,
} from "./utils"
import "./step-dialog.css"

/**
 * The field that holds a setting, in the settings: its own (`data-field`),
 * else the nearest that holds it (a sub-agent's row for its instruction,
 * a list for its row).
 */
function fieldFor(root: HTMLElement, key: string): HTMLElement | null {
  for (
    let at = key;
    at;
    at = at.includes(".") ? at.slice(0, at.lastIndexOf(".")) : ""
  ) {
    const found = root.querySelector<HTMLElement>(
      `[data-field="${CSS.escape(at)}"]`
    )
    if (found) return found
  }
  return null
}

function Section({
  title,
  className,
  children,
}: {
  title?: string
  className?: string
  children: React.ReactNode
}) {
  return (
    <section
      className={cn(
        "flex flex-col gap-4 border-t px-5 py-4 first:border-t-0",
        className
      )}
    >
      {title && (
        <h3 className="-mb-1 text-xs font-medium text-foreground">{title}</h3>
      )}
      {children}
    </section>
  )
}

/**
 * A step's settings, opened by clicking the step (or Enter on it): a card
 * as tall as the window over the dimmed canvas, where the step is set up
 * where the eye already is rather than in a panel off to the side. Changes
 * apply as they're made; there's nothing to save. A setting with a larger
 * editor (a schema's fields) opens it in a second card beside this one, and
 * the pair stays centred: the settings glide left to make room.
 */
export function StepDialog() {
  const api = useBuilderApi()
  const node = useBuilder((s) =>
    s.editing ? s.nodes.find((n) => n.id === s.editing) : undefined
  )
  // The last step shown stays while the dialog animates closed.
  const [shown, setShown] = React.useState(node)
  if (node && node !== shown) setShown(node)
  const popup = React.useRef<HTMLDivElement>(null)

  // The editor open beside the settings; another step, or none, starts without one.
  const [companion, setCompanion] = React.useState<string | null>(null)
  const [companionFor, setCompanionFor] = React.useState(node?.id)
  if (node?.id !== companionFor) {
    setCompanionFor(node?.id)
    setCompanion(null)
  }
  const [slot, setSlot] = React.useState<HTMLDivElement | null>(null)
  // Stable, so a setting's cleanup lets go only when it really goes.
  const actions = React.useMemo(
    () => ({
      show: (key: string) => withViewTransition(() => setCompanion(key)),
      hide: () => withViewTransition(() => setCompanion(null)),
      release: (key: string) =>
        setCompanion((open) => (open === key ? null : open)),
    }),
    []
  )
  const context = React.useMemo<Companion>(
    () => ({ slot, open: companion, ...actions }),
    [slot, companion, actions]
  )

  return (
    <Dialog
      open={Boolean(node)}
      onOpenChange={(open, details) => {
        if (open) return
        const target = details.event?.target
        // Completions and hovers float in the page's body, outside the dialog:
        // picking one isn't leaving it.
        if (
          details.reason === "outside-press" &&
          target instanceof Element &&
          target.closest(".cm-tooltip")
        ) {
          details.cancel()
          return
        }
        // Escape that closed a field's completions was the field's.
        if (details.reason === "escape-key" && details.event.defaultPrevented) {
          details.cancel()
          return
        }
        // With an editor open beside the settings, Escape or a click outside
        // closes that editor first, unsaved, and the settings stay.
        if (
          companion &&
          (details.reason === "escape-key" ||
            details.reason === "outside-press")
        ) {
          details.cancel()
          context.hide()
          return
        }
        api.getState().edit(null)
      }}
    >
      <DialogPortal>
        {/* It fades with the cards: shorter, it would be done (and gone) before them. */}
        <DialogOverlay className="bg-black/20 duration-150 supports-backdrop-filter:backdrop-blur-none dark:bg-black/45" />
        {/* The cards sit side by side, centred as a pair; around them is the backdrop. */}
        <DialogPrimitive.Popup
          ref={popup}
          initialFocus={popup}
          data-slot="dialog-content"
          className={cn(
            "pointer-events-none fixed inset-3 z-50 flex justify-center gap-3 outline-none",
            // Closing, they hold their faded end until removed rather than snap back.
            "duration-150 ease-out data-open:animate-in data-open:fade-in-0 data-open:slide-in-from-bottom-2 data-closed:animate-out data-closed:fade-out-0 data-closed:slide-out-to-bottom-1 data-closed:fill-mode-forwards"
          )}
        >
          <CompanionContext.Provider value={context}>
            <div
              className={cn(
                DIALOG_CARD,
                "w-[clamp(34rem,34vw,52rem)] max-w-full shrink-0 [view-transition-name:step-settings]",
                // Too narrow for two: the editor covers the settings.
                companion && "max-[1080px]:invisible"
              )}
            >
              {shown && <StepSettings key={shown.id} node={shown} />}
            </div>
            <div ref={setSlot} className="contents" />
          </CompanionContext.Provider>
        </DialogPrimitive.Popup>
      </DialogPortal>
    </Dialog>
  )
}

function StepSettings({ node }: { node: FlowNode }) {
  const { id, data } = node
  const adapter = useBuilder((s) => s.adapter)
  const ui = useBuilderUi()
  const { Fields, DataSection } = ui
  const info = adapter.kinds[data.kind]
  const lookups = useBuilder((s) => s.lookups)
  const allIssues = useBuilder((s) => s.issues)
  const issues = React.useMemo(
    () => allIssues.filter((i) => i.step === id),
    [allIssues, id]
  )
  const api = useBuilderApi()
  const nameId = React.useId()

  // An issue clicked: its setting comes into view, ready to fix. Two frames
  // on, so what it opens (a sub-agent's settings) has drawn.
  const focusField = useBuilder((s) => s.focusField)
  const body = React.useRef<HTMLDivElement>(null)
  React.useEffect(() => {
    if (!focusField) return
    let frame = requestAnimationFrame(() => {
      frame = requestAnimationFrame(() => {
        api.setState({ focusField: null })
        const field = body.current && fieldFor(body.current, focusField)
        if (!field) return
        field.scrollIntoView({
          block: "center",
          behavior: reducedMotion() ? "auto" : "smooth",
        })
        field
          .querySelector<HTMLElement>(
            "input, textarea, [contenteditable=true], button, [role=combobox]"
          )
          ?.focus({ preventScroll: true })
      })
    })
    return () => cancelAnimationFrame(frame)
  }, [focusField, api])

  return (
    <>
      <header className="flex items-start gap-3 border-b px-5 pt-4 pb-3.5">
        <KindGlyph info={info} icon={ui.iconOf?.(data)} />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <DialogTitle className="truncate text-sm leading-snug font-medium text-foreground">
            {data.name.trim() || info.label}
          </DialogTitle>
          <DialogDescription className="truncate text-2xs text-muted-foreground">
            {info.label} · {ui.detailOf(data, lookups)}
          </DialogDescription>
        </div>
        <DialogClose
          render={
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={`Close the ${ui.nouns.step}'s settings`}
              className="-mt-0.5 -mr-1.5 text-muted-foreground"
            />
          }
        >
          <Icon icon="close" />
        </DialogClose>
      </header>

      <div
        ref={body}
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain"
      >
        <Section className="gap-3">
          <p className="text-xs/[1.6] text-muted-foreground">{info.summary}</p>
          {issues.length > 0 && (
            <IssueList
              issues={issues}
              within
              // Each goes to its setting below.
              onOpen={(step, field) => api.getState().edit(step, field)}
            />
          )}
        </Section>

        <Section>
          {/* The start step has no name of its own: it's the start. */}
          {adapter.named(data.kind) && !ui.ownsName && (
            <Field>
              <FieldLabel htmlFor={nameId}>Name</FieldLabel>
              <Input
                id={nameId}
                value={data.name}
                onChange={(event) =>
                  api
                    .getState()
                    .updateStep(
                      id,
                      (step) => ({ ...step, name: event.target.value }),
                      "name"
                    )
                }
              />
            </Field>
          )}
          <FieldIssuesProvider issues={issues}>
            <Fields id={id} step={data} />
          </FieldIssuesProvider>
        </Section>

        {DataSection && (
          <Section title="Data">
            <DataSection id={id} step={data} />
          </Section>
        )}
      </div>

      <footer className="flex items-center gap-2 border-t px-5 py-3">
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            const store = api.getState()
            const copy = store.duplicateStep(id)
            if (copy) store.edit(copy)
          }}
        >
          <Icon icon="copy" data-icon="inline-start" />
          Duplicate
        </Button>
        <Button
          variant="destructive"
          size="sm"
          onClick={() => api.getState().removeSteps([id])}
        >
          Remove {ui.nouns.step}
        </Button>
        <span className="ml-auto text-2xs text-subtle max-[600px]:hidden">
          Changes apply as you make them
        </span>
        <DialogClose
          render={<Button size="sm" className="max-[600px]:ml-auto" />}
        >
          Done
        </DialogClose>
      </footer>
    </>
  )
}
