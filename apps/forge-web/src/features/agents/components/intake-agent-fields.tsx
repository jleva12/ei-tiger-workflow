import * as React from "react"
import { keepPreviousData, useQuery } from "@tanstack/react-query"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import {
  CheckField,
  ChoiceField,
  SwitchField,
  TextField,
} from "@/features/builder/components/fields/basic-fields"
import { ExpressionField } from "@/features/builder/components/fields/expression-field"
import { IssueMessages } from "@/features/builder/components/fields/field-issues"
import {
  useFieldIssues,
  useIssueLookup,
} from "@/features/builder/components/fields/field-issues-context"
import { FieldsReadOnly } from "@/features/builder/components/fields/read-only"
import {
  useCompanionCard,
  useOpenCompanion,
  useRevealCompanions,
} from "@/features/builder/components/settings-companion"
import {
  SettingsCompanion,
  SettingsHint,
} from "@/features/builder/components/settings-dialog"
import type { BuilderIssue } from "@/features/builder/lib/types"
import { previewIntake } from "@/features/agents/lib/api"
import {
  fieldGroups,
  fieldSetting,
  fieldTitle,
  FIELD_TYPE_LABELS,
  INTAKE_FIELD_TYPES,
  intakeScope,
  isChoice,
  keyFromLabel,
  newIntakeField,
  newIntakeSection,
  sampleValue,
  sectionSetting,
  type AnswerHow,
  type IntakePreview,
  type SampleAnswer,
} from "@/features/agents/lib/intake"
import {
  HANDS_OFF,
  ON_COMPLETE,
  TOOLS,
  type ChatConfigs,
  type ChatStep,
  type IntakeField,
  type IntakeFieldType,
  type IntakeSection,
} from "@/features/agents/lib/model"
import { RICH_ANSWER_ACTING } from "@/components/forge/generative-ui/catalog"
import { useChatBuilder } from "./chat-agent-store"

/*
 * An intake agent's settings: the record it collects, designed as fields in
 * sections rather than drawn as steps (the person picks the order; the
 * runtime works out what to ask next), how it may finish, what an agent
 * hands off to it with and gets back, and its model. Each field and section
 * opens in a card beside the settings; the preview opens one that runs
 * sample answers through the runtime's own engine (the admin API's
 * intake-preview). Everything changes as it's typed, one step of undo each.
 */

type Config = ChatConfigs["intake_agent"]

const fieldCard = (id: string) => `intake-field:${id}`
const sectionCard = (id: string) => `intake-section:${id}`
const PREVIEW_CARD = "intake-preview"

const code = (text: string) => (
  <code className="font-mono text-[0.9em]">{text}</code>
)

/** Changes to the design: each a step of undo, typing in one setting one step. */
function useDesign(id: string, config: Config) {
  const updateStep = useChatBuilder((s) => s.updateStep)
  return React.useMemo(() => {
    const patch = (values: Partial<Config>, key: string) =>
      updateStep(
        id,
        (step) =>
          ({ ...step, config: { ...step.config, ...values } }) as ChatStep,
        key
      )
    const setFields = (fields: IntakeField[], key = "fields") =>
      patch({ fields }, key)
    return {
      patch,
      setFields,
      updateField: (
        fieldId: string,
        values: Partial<IntakeField>,
        setting: string
      ) =>
        setFields(
          config.fields.map((f) =>
            f.id === fieldId ? { ...f, ...values } : f
          ),
          `fields.${fieldId}.${setting}`
        ),
      removeField: (fieldId: string) =>
        setFields(config.fields.filter((f) => f.id !== fieldId)),
      /** Moves a field before or after the next one in its section. */
      moveField: (fieldId: string, by: -1 | 1) => {
        const field = config.fields.find((f) => f.id === fieldId)
        if (!field) return
        const group = groupOf(field, config)
        const peers = config.fields.filter((f) => groupOf(f, config) === group)
        const at = peers.findIndex((f) => f.id === fieldId)
        const other = peers[at + by]
        if (!other) return
        const next = [...config.fields]
        const i = next.findIndex((f) => f.id === fieldId)
        const j = next.findIndex((f) => f.id === other.id)
        ;[next[i], next[j]] = [next[j], next[i]]
        setFields(next)
      },
      setSections: (sections: IntakeSection[], key = "sections") =>
        patch({ sections }, key),
      updateSection: (
        sectionId: string,
        values: Partial<IntakeSection>,
        setting: string
      ) =>
        patch(
          {
            sections: config.sections.map((s) =>
              s.id === sectionId ? { ...s, ...values } : s
            ),
          },
          `sections.${sectionId}.${setting}`
        ),
      /** A section gone: its fields stay, in none. */
      removeSection: (sectionId: string) =>
        patch(
          {
            sections: config.sections.filter((s) => s.id !== sectionId),
            fields: config.fields.map((f) =>
              f.section === sectionId ? { ...f, section: "" } : f
            ),
          },
          "sections"
        ),
      moveSection: (sectionId: string, by: -1 | 1) => {
        const at = config.sections.findIndex((s) => s.id === sectionId)
        const to = at + by
        if (at === -1 || to < 0 || to >= config.sections.length) return
        const next = [...config.sections]
        ;[next[at], next[to]] = [next[to], next[at]]
        patch({ sections: next }, "sections")
      },
    }
  }, [id, config, updateStep])
}

/** The group a field is listed in: its section's ID, or "" for none. */
function groupOf(field: IntakeField, config: Config) {
  return config.sections.some((s) => s.id === field.section)
    ? field.section
    : ""
}

/** The node's issues about one field or section, from what's open. */
function useIssuesUnder(setting: string): BuilderIssue[] {
  return useIssueLookup().under(setting)
}

/* -------------------------------------------------------------------------- */
/* The settings                                                               */
/* -------------------------------------------------------------------------- */

/**
 * An intake agent's settings. An issue clicked opens the card its setting
 * is in (a field's or a section's), or closes them for one of the node's own.
 */
export function IntakeAgentFields({
  id,
  config,
  model,
}: {
  id: string
  config: Config
  /** Its model settings, as the other agents show theirs. */
  model: React.ReactNode
}) {
  const reveal = useRevealCompanions()
  const focusField = useChatBuilder((s) => s.focusField)
  React.useEffect(() => {
    if (!focusField) return
    const [root, itemId] = focusField.split(".")
    if (root === "fields" && itemId) reveal([fieldCard(itemId)])
    else if (root === "sections" && itemId) reveal([sectionCard(itemId)])
    else reveal([])
  }, [focusField, reveal])

  const design = useDesign(id, config)
  return (
    <>
      <TextField
        issue="description"
        label="What it's for"
        value={config.description}
        multiline
        rows={2}
        placeholder="Takes a new claim: who it's for, what happened and when."
        onChange={(value) =>
          design.patch({ description: value }, "description")
        }
        description="The agent handing off reads this to know when to."
      />

      <RecordList id={id} config={config} />

      <Group title="How it finishes">
        <CheckField
          label="Reads back a summary first"
          checked={config.confirm_summary}
          onChange={(on) =>
            design.patch({ confirm_summary: on }, "confirm_summary")
          }
          description="Once everything's in, it reads back what it recorded and waits for the person's yes."
        />
        <CheckField
          label="The person can stop"
          checked={config.allow_cancel}
          onChange={(on) => design.patch({ allow_cancel: on }, "allow_cancel")}
          description="It hands back as cancelled, with what it had."
        />
        <CheckField
          label="It can escalate"
          checked={config.allow_escalate}
          onChange={(on) =>
            design.patch({ allow_escalate: on }, "allow_escalate")
          }
          description="When the person needs someone, or has something it can't record, it hands back as escalated with a note."
        />
        <p className="text-xs/[1.6] text-muted-foreground">
          Asked about something else, it always hands back as{" "}
          {code("interrupted")}; what the person gave is kept, and it picks up
          where it left off when it&apos;s handed off to again.
        </p>
      </Group>

      <HandOff id={id} config={config} />

      <Group title="Model and tone">
        {model}
        <p className="text-xs/[1.6] text-muted-foreground">
          Connect its <span className="font-medium text-foreground">Tools</span>{" "}
          way to what it can use as it asks: Rich answers to read the summary
          back as a table the person confirms with a click, a knowledge base for
          their questions, an HTTP tool to check a detail. It hands off to no
          one.
        </p>
      </Group>
    </>
  )
}

/** A group of settings with a title, ruled off from what's above. */
function Group({
  title,
  action,
  children,
}: {
  title: string
  action?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <div
      role="group"
      aria-label={title}
      className="-mx-5 flex flex-col gap-4 border-t px-5 pt-4"
    >
      <div className="-mb-1 flex items-center justify-between gap-2">
        <h3 className="text-xs font-medium text-foreground">{title}</h3>
        {action}
      </div>
      {children}
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* The record                                                                 */
/* -------------------------------------------------------------------------- */

/** One line about a field: what it is and when it's asked. */
function fieldLine(field: IntakeField): string {
  const parts: string[] = [
    isChoice(field.type) && field.options.length
      ? `${FIELD_TYPE_LABELS[field.type]} (${field.options.length})`
      : FIELD_TYPE_LABELS[field.type],
    field.required ? "Required" : "Optional",
  ]
  if (field.when.trim()) parts.push(`when ${field.when.trim()}`)
  if (field.infer) parts.push("may infer")
  if (field.confirm) parts.push("confirmed")
  if (field.prefill) parts.push("filled in on hand-off")
  return parts.join(" · ")
}

/**
 * The record as a list: its fields, by section, each opening its card; its
 * sections' heads opening theirs. New fields go at the end of a section.
 */
function RecordList({ id, config }: { id: string; config: Config }) {
  const design = useDesign(id, config)
  const open = useOpenCompanion()
  const readOnly = React.useContext(FieldsReadOnly)
  const preview = useCompanionCard(PREVIEW_CARD)
  const { issues, key } = useFieldIssues("fields")
  const groups = fieldGroups(config.fields, config.sections)

  const addField = (section: string) => {
    const field = newIntakeField(config.fields, section)
    design.setFields([...config.fields, field])
    open(fieldCard(field.id))
  }
  const addSection = () => {
    const section = newIntakeSection(config.sections)
    design.setSections([...config.sections, section])
    open(sectionCard(section.id))
  }

  return (
    <Group
      title="The record"
      action={
        <Button
          type="button"
          variant="outline"
          size="xs"
          aria-expanded={preview.open}
          disabled={!preview.canOpen}
          onClick={preview.toggle}
        >
          <Icon icon="view" data-icon="inline-start" />
          Preview
        </Button>
      }
    >
      <p className="-mt-2 text-xs/[1.6] text-muted-foreground">
        What it collects. The person can answer in any order; it asks for
        what&apos;s still needed, a section at a time, and only for what
        applies.
      </p>
      <div data-field={key} className="flex flex-col gap-3">
        {groups.map(({ section, fields }) => (
          <div key={section?.id ?? "loose"} className="flex flex-col gap-1.5">
            {section && (
              <SectionHead
                id={id}
                config={config}
                section={section}
                first={config.sections[0]?.id === section.id}
                last={config.sections.at(-1)?.id === section.id}
              />
            )}
            {fields.map((field, index) => (
              <FieldRow
                key={field.id}
                id={id}
                config={config}
                field={field}
                first={index === 0}
                last={index === fields.length - 1}
              />
            ))}
            {!readOnly && (
              <Button
                type="button"
                variant="ghost"
                size="xs"
                className="self-start text-muted-foreground"
                onClick={() => addField(section?.id ?? "")}
              >
                <Icon icon="plus" data-icon="inline-start" />
                {section
                  ? `Add a field to ${section.title.trim() || "this section"}`
                  : "Add a field"}
              </Button>
            )}
          </div>
        ))}
        <IssueMessages issues={issues} />
        {!readOnly && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={addSection}
            className="w-full border-dashed text-muted-foreground"
          >
            <Icon icon="plus" data-icon="inline-start" />
            Add a section
          </Button>
        )}
      </div>

      {config.fields.map((field) => (
        <FieldCard key={field.id} id={id} config={config} field={field} />
      ))}
      {config.sections.map((section) => (
        <SectionCard
          key={section.id}
          id={id}
          config={config}
          section={section}
        />
      ))}
      <PreviewCard id={id} config={config} />
    </Group>
  )
}

/** Move up, move down and remove, for a row. */
function RowActions({
  what,
  first,
  last,
  onMove,
  onRemove,
}: {
  what: string
  first: boolean
  last: boolean
  onMove: (by: -1 | 1) => void
  onRemove: () => void
}) {
  if (React.useContext(FieldsReadOnly)) return null
  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        aria-label={`Move ${what} up`}
        disabled={first}
        onClick={() => onMove(-1)}
      >
        <Icon icon="up" />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        aria-label={`Move ${what} down`}
        disabled={last}
        onClick={() => onMove(1)}
      >
        <Icon icon="down" />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        aria-label={`Remove ${what}`}
        onClick={onRemove}
        className="text-muted-foreground hover:text-destructive"
      >
        <Icon icon="close" />
      </Button>
    </>
  )
}

/** What a row says under its name: its first issue, else `line`. */
function RowLine({ issues, line }: { issues: BuilderIssue[]; line: string }) {
  const error = issues.some((i) => i.level === "error")
  if (!issues.length)
    return (
      <span className="truncate text-2xs text-muted-foreground">{line}</span>
    )
  return (
    <span
      className={cn(
        "flex items-center gap-1 truncate text-2xs",
        error ? "text-destructive" : "text-tone-amber-foreground"
      )}
    >
      <Icon
        icon={error ? "failed" : "warning"}
        size={12}
        className="shrink-0"
      />
      <span className="truncate">
        {issues.length > 1
          ? `${issues.length} to fix: ${issues[0].message}`
          : issues[0].message}
      </span>
    </span>
  )
}

function SectionHead({
  id,
  config,
  section,
  first,
  last,
}: {
  id: string
  config: Config
  section: IntakeSection
  first: boolean
  last: boolean
}) {
  const design = useDesign(id, config)
  const card = useCompanionCard(sectionCard(section.id))
  const setting = `sections.${section.id}`
  const issues = useIssuesUnder(setting)
  const { key } = useFieldIssues(setting)
  const title = section.title.trim() || "Untitled section"
  return (
    <div data-field={key} className="mt-1 flex items-center gap-1">
      <button
        type="button"
        aria-expanded={card.open}
        onClick={card.toggle}
        className={cn(
          "flex min-w-0 flex-1 flex-col rounded-(--radius-item) px-1 py-0.5 text-left transition-colors duration-150 hover:bg-muted",
          card.open && "bg-muted"
        )}
      >
        <span className="flex items-center gap-1.5 truncate text-2xs font-medium tracking-wide text-muted-foreground uppercase">
          {title}
        </span>
        <RowLine
          issues={issues}
          line={
            section.when.trim()
              ? `Unlocks when ${section.when.trim()}`
              : "Always asked"
          }
        />
      </button>
      <RowActions
        what={title}
        first={first}
        last={last}
        onMove={(by) => design.moveSection(section.id, by)}
        onRemove={() => design.removeSection(section.id)}
      />
    </div>
  )
}

function FieldRow({
  id,
  config,
  field,
  first,
  last,
}: {
  id: string
  config: Config
  field: IntakeField
  first: boolean
  last: boolean
}) {
  const design = useDesign(id, config)
  const card = useCompanionCard(fieldCard(field.id))
  const setting = `fields.${field.id}`
  const issues = useIssuesUnder(setting)
  const { key } = useFieldIssues(setting)
  const error = issues.some((i) => i.level === "error")
  return (
    <div
      data-field={key}
      className={cn(
        "flex items-center gap-1 rounded-(--radius-control) border py-1 pr-1 pl-1.5",
        error && "border-destructive"
      )}
    >
      <button
        type="button"
        aria-expanded={card.open}
        onClick={card.toggle}
        className={cn(
          "flex min-w-0 flex-1 items-center gap-2.5 rounded-(--radius-item) px-1 py-0.5 text-left transition-colors duration-150 hover:bg-muted",
          card.open && "bg-muted"
        )}
      >
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="flex min-w-0 items-baseline gap-2">
            <span className="truncate text-xs font-medium text-foreground">
              {fieldTitle(field)}
            </span>
            <code className="shrink-0 truncate font-mono text-2xs text-subtle">
              {field.key}
            </code>
          </span>
          <RowLine issues={issues} line={fieldLine(field)} />
        </span>
        <Icon icon="right" size={14} className="shrink-0 text-subtle" />
      </button>
      <RowActions
        what={fieldTitle(field)}
        first={first}
        last={last}
        onMove={(by) => design.moveField(field.id, by)}
        onRemove={() => design.removeField(field.id)}
      />
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* A field's and a section's cards                                            */
/* -------------------------------------------------------------------------- */

/** A card beside the settings that changes as it's typed in: nothing to save. */
function LiveCard({
  card,
  title,
  description,
  size = "sheet",
  children,
}: {
  card: string
  title: string
  description: React.ReactNode
  size?: "sheet" | "narrow" | "default"
  children: React.ReactNode
}) {
  const { hide } = useCompanionCard(card)
  const readOnly = React.useContext(FieldsReadOnly)
  return (
    <SettingsCompanion
      id={card}
      title={title}
      description={description}
      size={size}
      closeLabel={`Close ${title}`}
      bodyClassName="flex flex-col gap-4 px-5 py-4"
      footer={
        <>
          <SettingsHint>
            {readOnly ? "Read-only" : "Changes apply as you make them"}
          </SettingsHint>
          <Button
            type="button"
            size="sm"
            className="max-[600px]:ml-auto"
            onClick={hide}
          >
            Done
          </Button>
        </>
      }
    >
      {/* Read-only, every native field is disabled with it, as in the settings. */}
      <fieldset disabled={readOnly} className="contents">
        {children}
      </fieldset>
    </SettingsCompanion>
  )
}

const TYPE_OPTIONS = INTAKE_FIELD_TYPES.map((value) => ({
  value,
  label: FIELD_TYPE_LABELS[value],
}))

const NO_SECTION = "__none__"

function FieldCard({
  id,
  config,
  field,
}: {
  id: string
  config: Config
  field: IntakeField
}) {
  const design = useDesign(id, config)
  const set = <K extends keyof IntakeField>(
    setting: K,
    value: IntakeField[K]
  ) =>
    design.updateField(
      field.id,
      { [setting]: value } as Partial<IntakeField>,
      String(setting)
    )
  // The others, for its condition: a field's own value can't decide whether it's asked.
  const scope = React.useMemo(
    () => intakeScope(config.fields.filter((f) => f.id !== field.id)),
    [config.fields, field.id]
  )
  const others = config.fields
    .filter((f) => f.id !== field.id)
    .map((f) => f.key)
  const sections = [
    { value: NO_SECTION, label: "None" },
    ...config.sections.map((s) => ({
      value: s.id,
      label: s.title.trim() || "Untitled section",
    })),
  ]
  const { issues: whenIssues } = useFieldIssues(fieldSetting(field, "when"))
  return (
    <LiveCard
      card={fieldCard(field.id)}
      title={fieldTitle(field)}
      description="One field of the record: what it is, when it's asked and how sure the value must be."
    >
      <TextField
        issue={fieldSetting(field, "label")}
        label="Label"
        value={field.label}
        placeholder="Date of birth"
        onChange={(label) => {
          // A key that followed its label goes on following it.
          const followed = field.key === keyFromLabel(field.label, others)
          design.updateField(
            field.id,
            followed ? { label, key: keyFromLabel(label, others) } : { label },
            "label"
          )
        }}
        description="What people call it."
      />
      <TextField
        issue={fieldSetting(field, "key")}
        label="Key"
        code
        value={field.key}
        placeholder="date_of_birth"
        onChange={(key) => set("key", key)}
        description={
          <>
            What the record and the model call it, and what conditions read:
            lower-case letters, digits and {code("_")}.
          </>
        }
      />
      <ChoiceField
        issue={fieldSetting(field, "type")}
        label="Type"
        value={field.type}
        options={TYPE_OPTIONS}
        onChange={(type: IntakeFieldType) => set("type", type)}
        description={
          field.type === "date"
            ? "Kept as YYYY-MM-DD, whatever way the person says it."
            : undefined
        }
      />
      {isChoice(field.type) && (
        <OptionsField
          field={field}
          onChange={(options) => set("options", options)}
        />
      )}
      <SwitchField
        issue={fieldSetting(field, "required")}
        label="Required"
        checked={field.required}
        onChange={(on) => set("required", on)}
        description={
          field.required
            ? "It doesn't finish without it, when it applies."
            : "Offered once; the person can skip it."
        }
      />
      <ChoiceField
        issue={fieldSetting(field, "section")}
        label="Section"
        value={groupOf(field, config) || NO_SECTION}
        options={sections}
        onChange={(section: string) =>
          set("section", section === NO_SECTION ? "" : section)
        }
        description="Fields in a section are asked together, once it unlocks."
      />
      <ExpressionField
        label="Applies when"
        value={field.when}
        onChange={(when) => set("when", when)}
        mode="expression"
        scope={scope}
        check={{ expect: "condition" }}
        placeholder='employment = "employed"'
        description={
          <>
            Optional: a condition on the other fields, by key. It&apos;s only
            asked (and only handed back) while this is true. Empty: always.
          </>
        }
        // It says its own; ":field:" keeps the node's from saying them twice.
        issues={whenIssues.filter((i) => !i.id.includes(":field:"))}
        field={fieldSetting(field, "when")}
      />
      <TextField
        issue={fieldSetting(field, "description")}
        label="How to ask"
        value={field.description}
        multiline
        rows={2}
        placeholder="The policy number on their card, e.g. PN-1234."
        onChange={(value) => set("description", value)}
        description="What it is, and anything that helps the agent ask for it."
      />
      <TextField
        issue={fieldSetting(field, "why")}
        label="Why we ask"
        value={field.why}
        multiline
        rows={2}
        placeholder="We use it to find your policy."
        onChange={(value) => set("why", value)}
        description="What it says when the person asks why it's needed."
      />
      <div className="flex flex-col gap-3">
        <CheckField
          label="It may infer this"
          checked={field.infer}
          onChange={(on) => set("infer", on)}
          description="It may work the value out from what the person said; they confirm it before it finishes."
        />
        <CheckField
          label="The person confirms it"
          checked={field.confirm}
          onChange={(on) => set("confirm", on)}
          description="Even when they said it, it reads it back and waits for their yes."
        />
        <CheckField
          label="Filled in on hand-off"
          checked={field.prefill}
          onChange={(on) => set("prefill", on)}
          description="The agent handing off may pass it in, from what the person already told it, so it isn't asked again."
        />
      </div>
    </LiveCard>
  )
}

/** A choice's options: one a line. */
function OptionsField({
  field,
  onChange,
}: {
  field: IntakeField
  onChange: (options: string[]) => void
}) {
  const id = React.useId()
  const { issues, invalid, key } = useFieldIssues(
    fieldSetting(field, "options")
  )
  const [text, setText] = React.useState(field.options.join("\n"))
  const [shown, setShown] = React.useState(field.options)
  // Follow undo and redo, not the half-typed line.
  if (shown !== field.options) {
    setShown(field.options)
    const typed = text
      .split("\n")
      .map((o) => o.trim())
      .filter(Boolean)
    if (typed.join("\n") !== field.options.join("\n"))
      setText(field.options.join("\n"))
  }
  return (
    <Field data-invalid={invalid || undefined} data-field={key}>
      <FieldLabel htmlFor={id}>Options</FieldLabel>
      <Textarea
        id={id}
        aria-invalid={invalid || undefined}
        value={text}
        placeholder={"Auto\nHome\nTravel"}
        onChange={(event) => {
          setText(event.target.value)
          onChange(
            event.target.value
              .split("\n")
              .map((o) => o.trim())
              .filter(Boolean)
          )
        }}
        style={{ minHeight: "5.5rem" }}
      />
      {issues.length ? (
        <IssueMessages issues={issues} />
      ) : (
        <FieldDescription>
          One a line. The person may say it any way; it&apos;s kept as written
          here.
        </FieldDescription>
      )}
    </Field>
  )
}

function SectionCard({
  id,
  config,
  section,
}: {
  id: string
  config: Config
  section: IntakeSection
}) {
  const design = useDesign(id, config)
  const scope = React.useMemo(() => intakeScope(config.fields), [config.fields])
  const { issues: whenIssues } = useFieldIssues(sectionSetting(section, "when"))
  const fields = config.fields.filter((f) => f.section === section.id)
  return (
    <LiveCard
      card={sectionCard(section.id)}
      title={section.title.trim() || "Untitled section"}
      description="Fields asked together, once the section unlocks."
      size="narrow"
    >
      <TextField
        issue={sectionSetting(section, "title")}
        label="Title"
        value={section.title}
        placeholder="Vehicle details"
        onChange={(title) =>
          design.updateSection(section.id, { title }, "title")
        }
      />
      <ExpressionField
        label="Unlocks when"
        value={section.when}
        onChange={(when) => design.updateSection(section.id, { when }, "when")}
        mode="expression"
        scope={scope}
        check={{ expect: "condition" }}
        placeholder='claim_type = "Auto"'
        description="Optional: a condition on the record's fields, by key. Until it's true, none of its fields are asked. Empty: always."
        issues={whenIssues.filter((i) => !i.id.includes(":field:"))}
        field={sectionSetting(section, "when")}
      />
      <p className="text-xs/[1.6] text-muted-foreground">
        {fields.length
          ? `${fields.length === 1 ? "1 field" : `${fields.length} fields`}: ${fields.map(fieldTitle).join(", ")}.`
          : "No fields yet: add them from the record's list, or move one here from its card."}
      </p>
    </LiveCard>
  )
}

/* -------------------------------------------------------------------------- */
/* Handing off                                                                */
/* -------------------------------------------------------------------------- */

/** What an agent hands off with, what it gets back, and what starts when it completes. */
function HandOff({ id, config }: { id: string; config: Config }) {
  const filled = config.fields.filter((f) => f.prefill)
  // The workflows on its On complete way, by the names they have.
  const starts = useChatBuilder((s) =>
    s.edges
      .filter((e) => e.source === id && e.sourceHandle === ON_COMPLETE)
      .map((e) => {
        const node = s.nodes.find((n) => n.id === e.target)?.data
        if (node?.kind !== "adk_workflow") return node?.name ?? ""
        return s.lookups.workflows[node.config.workflow] ?? node.name
      })
      .filter(Boolean)
      .join(", ")
  )
  const ends = [
    "completed",
    ...(config.allow_cancel ? ["cancelled"] : []),
    ...(config.allow_escalate ? ["escalated"] : []),
    "interrupted",
  ]
  const called = useChatBuilder((s) =>
    s.edges.some((e) => e.target === id && e.sourceHandle === TOOLS)
  )
  const handedTo = useChatBuilder((s) =>
    s.edges.some((e) => e.target === id && e.sourceHandle === HANDS_OFF)
  )
  const strong = (words: string) => (
    <span className="font-medium text-foreground">{words}</span>
  )
  return (
    <Group title="How agents use it">
      <p className="-mt-2 text-xs/[1.6] text-muted-foreground">
        {strong("Handed off to.")} Connect an agent&apos;s{" "}
        {strong("Hands off to")} way to it. That agent calls it with what it
        already knows, the person talks with it until it&apos;s done, then the
        agent carries on with what it hands back.
      </p>
      <p className="text-xs/[1.6] text-muted-foreground">
        {strong("As a tool.")} Connect an agent&apos;s {strong("Tools")} way to
        it, beside other intakes. That agent asks the questions itself and files
        a request to each intake the person needs: a field with the same key in
        several is asked once. Requests are kept for the person, so an open one
        can be picked up in another conversation; submitting one starts the
        workflows on its On complete way. Its own Tools aren&apos;t used this
        way.
        {called && handedTo ? " Agents use it both ways." : ""}
      </p>
      <dl className="flex flex-col rounded-(--radius-control) border text-xs">
        <div className="flex items-baseline gap-3 px-2.5 py-1.5">
          <dt className="w-24 shrink-0 text-muted-foreground">Sent in</dt>
          <dd className="min-w-0 font-mono text-2xs text-foreground">
            {["background", ...filled.map((f) => f.key)].join(", ")}
          </dd>
        </div>
        <div className="flex items-baseline gap-3 border-t px-2.5 py-1.5">
          <dt className="w-24 shrink-0 text-muted-foreground">Handed back</dt>
          <dd className="min-w-0 font-mono text-2xs text-foreground">
            status, note, record, missing, workflows
          </dd>
        </div>
        <div className="flex items-baseline gap-3 border-t px-2.5 py-1.5">
          <dt className="w-24 shrink-0 text-muted-foreground">On complete</dt>
          <dd className="min-w-0 text-2xs text-foreground">
            {starts ? (
              <>Starts {starts} with the record</>
            ) : (
              <span className="text-muted-foreground">
                Connect its On complete way to a workflow to start it with the
                record
              </span>
            )}
          </dd>
        </div>
        <div className="flex items-baseline gap-3 border-t px-2.5 py-1.5">
          <dt className="w-24 shrink-0 text-muted-foreground">Ends as</dt>
          <dd className="min-w-0 font-mono text-2xs text-foreground">
            {ends.join(", ")}
          </dd>
        </div>
      </dl>
    </Group>
  )
}

/* -------------------------------------------------------------------------- */
/* The preview                                                                */
/* -------------------------------------------------------------------------- */

const HOWS: { value: AnswerHow; label: string }[] = [
  { value: "stated", label: "Said" },
  { value: "inferred", label: "Inferred" },
  { value: "handoff", label: "Handed in" },
  { value: "confirmed", label: "Confirmed" },
  { value: "skipped", label: "Skipped" },
]

/** A value that settles `delay` ms after it last changed. */
function useSettled<T>(value: T, delay = 300): T {
  const [settled, setSettled] = React.useState(value)
  React.useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), delay)
    return () => window.clearTimeout(timer)
  }, [value, delay])
  return settled
}

type Sample = { text: string; how: AnswerHow }

/** A pick's "no answer", as a Select's value. */
const NO_ANSWER = "__none__"

/**
 * Sample answers, run through the runtime's own engine: what it would ask
 * next, what's missing or waiting for a yes, what its model would be told
 * and what it would hand back. Nothing is saved or run.
 */
function PreviewCard({ id, config }: { id: string; config: Config }) {
  const organizationId = useChatBuilder((s) => s.meta.organization_id)
  const name = useChatBuilder(
    (s) => s.nodes.find((n) => n.id === id)?.data.name ?? "Intake"
  )
  // With Rich answers on its Tools way, it reads the summary back as a
  // table; with controls among their components, it may ask with a form too.
  const presents = useChatBuilder((s) =>
    s.edges.some(
      (e) =>
        e.source === id &&
        e.sourceHandle === TOOLS &&
        s.nodes.find((n) => n.id === e.target)?.data.kind === "rich_answers"
    )
  )
  const presentForms = useChatBuilder((s) =>
    s.edges.some((e) => {
      const target = s.nodes.find((n) => n.id === e.target)?.data
      return (
        e.source === id &&
        e.sourceHandle === TOOLS &&
        target?.kind === "rich_answers" &&
        target.config.components.some((name) =>
          (RICH_ANSWER_ACTING as readonly string[]).includes(name)
        )
      )
    })
  )
  const { open } = useCompanionCard(PREVIEW_CARD)
  const [samples, setSamples] = React.useState<Record<string, Sample>>({})
  const [confirmed, setConfirmed] = React.useState(false)

  const answers = React.useMemo(() => {
    const out: Record<string, SampleAnswer> = {}
    for (const field of config.fields) {
      const sample = samples[field.id]
      if (!sample) continue
      if (sample.how === "skipped")
        out[field.key] = { value: null, how: "skipped" }
      else if (sample.text.trim())
        out[field.key] = {
          value: sampleValue(field, sample.text),
          how: sample.how,
        }
    }
    return out
  }, [samples, config.fields])
  const asked = React.useMemo(
    () => ({
      name,
      config: {
        ...config,
        presents,
        present_forms: presentForms,
      } as unknown as Record<string, unknown>,
      answers,
      summary_confirmed: confirmed,
    }),
    [name, config, presents, presentForms, answers, confirmed]
  )
  const request = useSettled(asked)
  const preview = useQuery({
    queryKey: ["intake-preview", organizationId, request],
    queryFn: () => previewIntake(organizationId, request),
    enabled: open && Boolean(organizationId),
    placeholderData: keepPreviousData,
    staleTime: Infinity,
    retry: false,
  })
  const data = preview.data
  const byKey = new Map(config.fields.map((f) => [f.key, f]))
  const titles = (keys: string[] | undefined) =>
    (keys ?? []).map((k) => (byKey.get(k) ? fieldTitle(byKey.get(k)!) : k))
  const locked = new Set(data?.locked ?? [])
  const applying = new Set(data?.applying ?? [])

  return (
    <SettingsCompanion
      id={PREVIEW_CARD}
      title="Preview"
      description="Answer as a person might, in any order, and see what it would ask next and what it would hand back. It runs the intake's own engine: nothing is saved or run."
      icon="view"
      closeLabel="Close the preview"
      bodyClassName="flex flex-col gap-5 px-5 py-4"
      footer={
        <>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => {
              setSamples({})
              setConfirmed(false)
            }}
          >
            Clear answers
          </Button>
          <SettingsHint>
            {preview.isFetching ? "Working it out…" : "Nothing is saved"}
          </SettingsHint>
        </>
      }
    >
      <Standing data={data} titles={titles} error={preview.error} />

      <section className="flex flex-col gap-3">
        <h3 className="text-xs font-medium text-foreground">Sample answers</h3>
        {fieldGroups(config.fields, config.sections).map(
          ({ section, fields }) => (
            <div
              key={section?.id ?? "loose"}
              className={cn(
                "flex flex-col gap-1.5",
                section && locked.has(section.id) && "opacity-60"
              )}
            >
              {section && (
                <span className="text-2xs font-medium tracking-wide text-muted-foreground uppercase">
                  {section.title.trim() || "Untitled section"}
                  {locked.has(section.id) &&
                    ` · locked until ${section.when.trim()}`}
                </span>
              )}
              {fields.map((field) => (
                <SampleRow
                  key={field.id}
                  field={field}
                  sample={samples[field.id] ?? { text: "", how: "stated" }}
                  onChange={(sample) =>
                    setSamples((current) => ({
                      ...current,
                      [field.id]: sample,
                    }))
                  }
                  next={data?.ask_next?.includes(field.key) ?? false}
                  applies={!data || applying.has(field.key)}
                  waiting={data?.unconfirmed?.includes(field.key) ?? false}
                  refused={data?.refused?.[field.key]}
                />
              ))}
            </div>
          )
        )}
        {!config.fields.length && (
          <p className="text-xs/[1.6] text-muted-foreground">
            Add fields to the record to try it.
          </p>
        )}
        {config.confirm_summary && (
          <CheckField
            label="The person said yes to the summary"
            checked={confirmed}
            onChange={setConfirmed}
            description="Only counts once everything's in."
          />
        )}
      </section>

      {data?.brief && (
        <Disclosure title="What its model is told">
          <pre className="max-h-96 overflow-auto rounded-(--radius-control) bg-muted p-3 font-mono text-2xs/[1.6] whitespace-pre-wrap text-foreground">
            {data.brief}
          </pre>
        </Disclosure>
      )}
      {data?.result && (
        <Disclosure title="What it would hand back">
          <pre className="max-h-72 overflow-auto rounded-(--radius-control) bg-muted p-3 font-mono text-2xs/[1.6] whitespace-pre-wrap text-foreground">
            {JSON.stringify(
              { ...data.result, status: data.done ? "completed" : "…" },
              null,
              2
            )}
          </pre>
        </Disclosure>
      )}
    </SettingsCompanion>
  )
}

/** Where the sample record stands: done, or what's asked next and what's missing. */
function Standing({
  data,
  titles,
  error,
}: {
  data: IntakePreview | undefined
  titles: (keys: string[] | undefined) => string[]
  error: unknown
}) {
  if (error)
    return (
      <p className="flex items-start gap-2 text-xs/[1.6] text-destructive">
        <Icon icon="warning" size={14} className="mt-0.5 shrink-0" />
        <span>
          The preview couldn&apos;t be worked out:{" "}
          {error instanceof Error ? error.message : String(error)}
        </span>
      </p>
    )
  if (!data) return null
  if (data.problems.length)
    return (
      <ul className="flex flex-col gap-1">
        {data.problems.map((problem) => (
          <li
            key={problem}
            className="flex items-start gap-2 text-xs/[1.6] text-destructive"
          >
            <Icon icon="warning" size={14} className="mt-0.5 shrink-0" />
            <span>{problem}</span>
          </li>
        ))}
      </ul>
    )
  const rows: [string, string][] = []
  if (data.done) rows.push(["Status", "Done: it would hand back the record"])
  else if (data.ready)
    rows.push(["Status", "Everything's in: it reads back the summary"])
  if (data.ask_next?.length)
    rows.push(["Asks next", titles(data.ask_next).join(", ")])
  if (data.missing?.length)
    rows.push(["Still needed", titles(data.missing).join(", ")])
  if (data.unconfirmed?.length)
    rows.push(["Waiting for a yes", titles(data.unconfirmed).join(", ")])
  return (
    <dl
      className={cn(
        "flex flex-col rounded-(--radius-control) border text-xs",
        data.done && "border-transparent bg-success-surface"
      )}
    >
      {rows.map(([label, value], index) => (
        <div
          key={label}
          className={cn(
            "flex items-baseline gap-3 px-2.5 py-1.5",
            index > 0 && "border-t"
          )}
        >
          <dt className="w-28 shrink-0 text-muted-foreground">{label}</dt>
          <dd className="min-w-0 text-foreground">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

function SampleRow({
  field,
  sample,
  onChange,
  next,
  applies,
  waiting,
  refused,
}: {
  field: IntakeField
  sample: Sample
  onChange: (sample: Sample) => void
  /** It's among what would be asked next. */
  next: boolean
  /** Its condition (and its section's) holds. */
  applies: boolean
  /** Its value waits for the person's yes. */
  waiting: boolean
  /** Why the runtime wouldn't take the value. */
  refused?: string
}) {
  const id = React.useId()
  const options = field.options.map((o) => o.trim()).filter(Boolean)
  const skipped = sample.how === "skipped"
  const picks =
    field.type === "boolean"
      ? [
          { value: "yes", label: "Yes" },
          { value: "no", label: "No" },
        ]
      : options.map((o) => ({ value: o, label: o }))
  const value =
    field.type === "boolean" || field.type === "choice" ? (
      <Select
        items={[{ value: NO_ANSWER, label: "No answer" }, ...picks]}
        value={sample.text || NO_ANSWER}
        onValueChange={(text) =>
          onChange({
            ...sample,
            text: !text || text === NO_ANSWER ? "" : String(text),
          })
        }
        disabled={skipped}
      >
        <SelectTrigger id={id} size="sm" className="w-full min-w-0">
          <SelectValue />
        </SelectTrigger>
        <SelectContent alignItemWithTrigger={false}>
          <SelectItem value={NO_ANSWER}>No answer</SelectItem>
          {picks.map((pick) => (
            <SelectItem key={pick.value} value={pick.value}>
              {pick.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    ) : (
      <Input
        id={id}
        value={sample.text}
        disabled={skipped}
        placeholder={
          field.type === "choices"
            ? options.slice(0, 2).join(", ") || "One, another"
            : field.type === "date"
              ? "2026-03-14"
              : "No answer"
        }
        aria-invalid={Boolean(refused) || undefined}
        onChange={(event) => onChange({ ...sample, text: event.target.value })}
        className="h-7 text-xs"
      />
    )
  return (
    <div
      className={cn(
        "flex flex-col gap-1 rounded-(--radius-control) border px-2 py-1.5",
        next && "border-foreground/40",
        !applies && "opacity-60"
      )}
    >
      <div className="flex items-center gap-2">
        <label
          htmlFor={id}
          className="min-w-0 flex-1 truncate text-xs font-medium text-foreground"
        >
          {fieldTitle(field)}
          {!field.required && (
            <span className="ml-1.5 text-2xs font-normal text-subtle">
              optional
            </span>
          )}
        </label>
        {next && <Marker>Asked next</Marker>}
        {waiting && <Marker tone="amber">Needs a yes</Marker>}
        {!applies && <Marker tone="muted">Doesn&apos;t apply</Marker>}
      </div>
      <div className="flex items-center gap-1.5">
        <div className="min-w-0 flex-1">{value}</div>
        <Select
          items={HOWS.filter((h) => h.value !== "skipped" || !field.required)}
          value={sample.how}
          onValueChange={(how) =>
            how && onChange({ ...sample, how: how as AnswerHow })
          }
        >
          <SelectTrigger
            size="sm"
            className="w-28 shrink-0"
            aria-label={`How ${fieldTitle(field)} was given`}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent alignItemWithTrigger={false}>
            {HOWS.filter((h) => h.value !== "skipped" || !field.required).map(
              (h) => (
                <SelectItem key={h.value} value={h.value}>
                  {h.label}
                </SelectItem>
              )
            )}
          </SelectContent>
        </Select>
      </div>
      {refused && (
        <span className="text-2xs text-destructive">
          It wouldn&apos;t take this: {refused}.
        </span>
      )}
    </div>
  )
}

function Marker({
  tone = "ink",
  children,
}: {
  tone?: "ink" | "amber" | "muted"
  children: React.ReactNode
}) {
  return (
    <span
      className={cn(
        "shrink-0 rounded-(--radius-chip) px-1.5 py-px text-3xs font-medium",
        tone === "ink" && "bg-foreground text-background",
        tone === "amber" && "bg-tone-amber text-tone-amber-foreground",
        tone === "muted" && "bg-muted text-muted-foreground"
      )}
    >
      {children}
    </span>
  )
}

/** A heading that shows what's under it when opened. */
function Disclosure({
  title,
  children,
}: {
  title: string
  children: React.ReactNode
}) {
  const [open, setOpen] = React.useState(false)
  return (
    <section className="flex flex-col gap-2">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 self-start rounded-(--radius-item) text-xs font-medium text-foreground"
      >
        <Icon
          icon="right"
          size={14}
          className={cn(
            "text-subtle transition-transform duration-150",
            open && "rotate-90"
          )}
        />
        {title}
      </button>
      {open && children}
    </section>
  )
}
