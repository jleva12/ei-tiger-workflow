import * as React from "react"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { DialogClose } from "@/components/ui/dialog"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { FieldIssuesProvider } from "@/features/builder/components/fields/field-issues"
import { FieldsReadOnly } from "./fields/read-only"
import { KindGlyph } from "./glyph"
import { IssueList } from "./issue-list"
import {
  SettingsBody,
  SettingsDialog,
  SettingsFooter,
  SettingsHeader,
  SettingsHint,
  SettingsSection as Section,
} from "./settings-dialog"
import { useBuilder, useBuilderApi, type FlowNode } from "./store"
import { useBuilderUi } from "./ui"
import { reducedMotion } from "./utils"

/**
 * The field that holds a setting, in the dialog's cards: its own
 * (`data-field`), else (`nearest`) the nearest that holds it (a sub-agent's
 * row for its instruction, a list for its row). The deepest card's comes
 * first: the cards before it may be folded.
 */
function fieldFor(
  root: HTMLElement,
  key: string,
  nearest: boolean
): HTMLElement | null {
  for (
    let at = key;
    at;
    at = nearest && at.includes(".") ? at.slice(0, at.lastIndexOf(".")) : ""
  ) {
    const found = root.querySelectorAll<HTMLElement>(
      `[data-field="${CSS.escape(at)}"]`
    )
    if (found.length) return found[found.length - 1]
  }
  return null
}

/** Frames to wait for a setting's own field, while the cards it's in open. */
const FIELD_WAIT_FRAMES = 12

/**
 * A step's settings, opened by clicking the step (or Enter on it): a card
 * as tall as the window over the dimmed canvas, where the step is set up
 * where the eye already is rather than in a panel off to the side. Changes
 * apply as they're made; there's nothing to save. A setting with a larger
 * editor (a schema's fields) opens it in a card beside this one, and that
 * card can open the next, up to four (settings-dialog.tsx): the row stays
 * centred, and the earlier cards fold into spines. Another step, or none,
 * starts without them: the settings they were opened from go, and take
 * their cards with them.
 */
export function StepDialog() {
  const api = useBuilderApi()
  const node = useBuilder((s) =>
    s.editing ? s.nodes.find((n) => n.id === s.editing) : undefined
  )
  // The last step shown stays while the dialog animates closed.
  const [shown, setShown] = React.useState(node)
  if (node && node !== shown) setShown(node)

  return (
    <SettingsDialog
      open={Boolean(node)}
      onOpenChange={(open) => {
        if (!open) api.getState().edit(null)
      }}
    >
      {shown && <StepSettings key={shown.id} node={shown} />}
    </SettingsDialog>
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
  const readOnly = useBuilder((s) => s.readOnly)
  const nameId = React.useId()

  // An issue clicked: its setting comes into view, ready to fix. Its own
  // field is waited for a few frames, while what it opens (a sub-agent's
  // card beside these settings) draws; then the nearest that holds it.
  const focusField = useBuilder((s) => s.focusField)
  const body = React.useRef<HTMLDivElement>(null)
  React.useEffect(() => {
    if (!focusField) return
    let waited = 0
    const look = () => {
      // The cards beside the settings sit next to them, not in their body.
      const root =
        body.current?.closest<HTMLElement>('[data-slot="dialog-content"]') ??
        body.current
      const own = root && fieldFor(root, focusField, false)
      if (!own && waited++ < FIELD_WAIT_FRAMES) {
        frame = requestAnimationFrame(look)
        return
      }
      api.setState({ focusField: null })
      const field = own ?? (root && fieldFor(root, focusField, true))
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
    }
    // Two frames on at least, so what it opens has drawn.
    let frame = requestAnimationFrame(() => {
      frame = requestAnimationFrame(look)
    })
    return () => cancelAnimationFrame(frame)
  }, [focusField, api])

  return (
    <>
      <SettingsHeader
        glyph={<KindGlyph info={info} />}
        title={data.name.trim() || info.label}
        description={`${info.label} · ${ui.detailOf(data, lookups)}`}
        closeLabel={`Close the ${ui.nouns.step}'s settings`}
      />

      <SettingsBody ref={body}>
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
                disabled={readOnly}
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
            <FieldsReadOnly.Provider value={readOnly}>
              {/* Read-only, every native field is disabled with it. */}
              <fieldset disabled={readOnly} className="contents">
                <Fields id={id} step={data} />
              </fieldset>
            </FieldsReadOnly.Provider>
          </FieldIssuesProvider>
        </Section>

        {DataSection && (
          <Section title="Data">
            <DataSection id={id} step={data} />
          </Section>
        )}
      </SettingsBody>

      <SettingsFooter>
        {!readOnly && (
          <>
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
          </>
        )}
        <SettingsHint>
          {readOnly ? (ui.readOnlyHint ?? "Read-only") : "Changes apply as you make them"}
        </SettingsHint>
        <DialogClose
          render={<Button size="sm" className="max-[600px]:ml-auto" />}
        >
          Done
        </DialogClose>
      </SettingsFooter>
    </>
  )
}
