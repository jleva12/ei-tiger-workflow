import * as React from "react"
import { LockIcon } from "@hugeicons/core-free-icons"

import { IssueMessages } from "@/features/builder/components/fields/field-issues"
import { useFieldIssues } from "@/features/builder/components/fields/field-issues-context"
import { useCompanionCard } from "@/features/builder/components/settings-companion"
import { SettingsCompanion } from "@/features/builder/components/settings-dialog"
import { JsonSchemaBuilder } from "@/features/json/components/json-schema-builder"
import {
  buildJsonSchema,
  emptyDraft,
  parseJsonSchema,
  type SchemaDraft,
} from "@/features/json/lib/json-schema"
import { typeLabel } from "@/features/steps/lib/types"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  declaredState,
  FIXED_STATE_FIELDS,
  REQUEST_FIELDS,
  stateNameProblem,
  type InputField,
} from "@/features/agents/lib/input"

const SUBJECT = { one: "state field", many: "state fields" }
const FIXED = REQUEST_FIELDS.length + FIXED_STATE_FIELDS.length

/**
 * The chat agent's input: what a chat sends to run it (ADK's run request).
 * The request's fields and the model picker's state are fixed; the agent
 * declares the rest of the state it's sent, in a card beside the settings,
 * and its instructions read all of it.
 */
export function InputSchemaField({
  schema,
  onChange,
}: {
  /** The state it declares, as a JSON Schema; `{}` declares none. */
  schema: Record<string, unknown>
  onChange: (schema: Record<string, unknown>) => void
}) {
  const { issues, key: field } = useFieldIssues("state_schema")
  const key = React.useId()
  const editor = useCompanionCard(key)
  const [draft, setDraft] = React.useState<SchemaDraft>(emptyDraft)
  const [warnings, setWarnings] = React.useState<string[]>([])
  const { fields, required } = declaredState(schema)
  const problems = draft.properties
    .map((p) => p.name.trim() && stateNameProblem(p.name.trim()))
    .filter((p): p is string => Boolean(p))

  const edit = () => {
    const parsed =
      Object.keys(schema).length > 0
        ? parseJsonSchema(schema)
        : { draft: emptyDraft(), warnings: [] }
    setDraft(parsed.draft)
    setWarnings(parsed.warnings)
    editor.show()
  }

  return (
    <div
      role="group"
      aria-label="Input schema"
      data-field={field}
      className="flex flex-col gap-2"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium text-foreground">
          Input schema
        </span>
        <Button
          type="button"
          variant="outline"
          size="xs"
          aria-expanded={editor.open}
          disabled={!editor.canOpen}
          onClick={edit}
        >
          <Icon icon="code" data-icon="inline-start" />
          Edit input
        </Button>
      </div>
      <ul className="flex flex-col rounded-(--radius-control) border">
        <li className="flex items-center gap-2 px-2.5 py-1.5 text-xs text-muted-foreground">
          <Icon icon={LockIcon} size={12} className="shrink-0 text-subtle" />
          <span className="min-w-0 truncate">
            {FIXED} fixed fields: the run request, model and thinking_level
          </span>
        </li>
        {fields.map(([name, type]) => (
          <li
            key={name}
            className="flex items-baseline gap-3 border-t px-2.5 py-1.5"
          >
            <code className="min-w-0 truncate font-mono text-xs text-foreground">
              state.{name}
            </code>
            {required.has(name) && (
              <span className="text-2xs text-subtle">required</span>
            )}
            <span className="ml-auto shrink-0 font-mono text-2xs text-muted-foreground">
              {typeLabel(type)}
            </span>
          </li>
        ))}
      </ul>
      {!fields.length && (
        <p className="text-xs/[1.6] text-muted-foreground">
          Declare the state your chat sends with each message, and its
          instructions can read it as{" "}
          <code className="font-mono text-[0.9em]">{"{{ state.name }}"}</code>.
        </p>
      )}
      <IssueMessages issues={issues} />

      {/* Gone while open (its node's settings changed), it closes too. */}
      <SettingsCompanion
        id={key}
        title="Input schema"
        description={
          <>
            What a chat sends to run this agent: ADK&apos;s run request. Its
            fields and the model picker&apos;s state are always sent; declare
            the other state your chat sends in{" "}
            <code className="font-mono text-[0.9em]">stateDelta</code>.
            Instructions read them as{" "}
            <code className="font-mono text-[0.9em]">
              {"{{ request.userId }}"}
            </code>{" "}
            and{" "}
            <code className="font-mono text-[0.9em]">{"{{ state.name }}"}</code>
            .
          </>
        }
        closeLabel="Close without saving"
        render={
          <form
            onSubmit={(event) => {
              event.preventDefault()
              // Empty and open is the same as undeclared.
              onChange(
                draft.properties.length || !draft.additionalProperties
                  ? buildJsonSchema(draft)
                  : {}
              )
              editor.hide()
            }}
          />
        }
        bodyClassName="flex flex-col gap-5 px-5 py-4"
        footer={
          <>
            <span className="flex items-center gap-1.5 text-2xs text-subtle max-[600px]:hidden">
              <Icon icon="code" size={14} />
              Nothing changes until you save
            </span>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="ml-auto"
              onClick={() => editor.hide()}
            >
              Cancel
            </Button>
            <Button type="submit" size="sm">
              Save input
            </Button>
          </>
        }
      >
        <FixedFields
          title="Request"
          description="The run request's own fields. Every chat sends them."
          root="request"
          fields={REQUEST_FIELDS}
        />
        <FixedFields
          title="stateDelta"
          description="The state sent with each message. The model picker always sends these two."
          root="state"
          fields={FIXED_STATE_FIELDS}
        />
        {warnings.length > 0 && (
          <p className="flex items-start gap-2 text-xs/[1.6] text-tone-amber-foreground">
            <Icon icon="warning" size={14} className="mt-0.5 shrink-0" />
            <span>
              Some of it can&apos;t be edited here and is left out when you
              save: {warnings.join("; ")}.
            </span>
          </p>
        )}
        {problems.length > 0 && (
          <ul className="flex flex-col gap-1">
            {problems.map((problem) => (
              <li
                key={problem}
                className="flex items-start gap-2 text-xs/[1.6] text-destructive"
              >
                <Icon icon="warning" size={14} className="mt-0.5 shrink-0" />
                <span>{problem}</span>
              </li>
            ))}
          </ul>
        )}
        <JsonSchemaBuilder
          value={draft}
          onChange={setDraft}
          subject={SUBJECT}
          title="Your state"
          description="The other state your chat sends in stateDelta, each with its type and whether it's always there. Instructions complete and check against these."
        />
      </SettingsCompanion>
    </div>
  )
}

/** Fields the agent always gets: shown, never edited. */
function FixedFields({
  title,
  description,
  root,
  fields,
}: {
  title: string
  description: string
  /** What instructions read them under. */
  root: string
  fields: InputField[]
}) {
  return (
    <section className="flex flex-col gap-2">
      <div className="flex flex-col gap-0.5">
        <h3 className="flex items-center gap-1.5 text-xs font-medium text-foreground">
          {title}
          <span className="flex items-center gap-1 text-2xs font-normal text-subtle">
            <Icon icon={LockIcon} size={11} />
            Fixed
          </span>
        </h3>
        <p className="text-xs/[1.6] text-muted-foreground">{description}</p>
      </div>
      <ul className="flex flex-col rounded-(--radius-control) border bg-muted/40">
        {fields.map((field) => (
          <li
            key={field.name}
            aria-readonly="true"
            className="flex items-baseline gap-3 border-t px-2.5 py-1.5 first:border-t-0"
          >
            <code className="shrink-0 font-mono text-xs text-foreground">
              {field.name}
            </code>
            <span className="min-w-0 truncate text-2xs text-muted-foreground">
              {field.detail} ·{" "}
              <code className="font-mono">{`${root}.${field.name}`}</code>
            </span>
            <span className="ml-auto shrink-0 font-mono text-2xs text-muted-foreground">
              {typeLabel(field.type)}
            </span>
          </li>
        ))}
      </ul>
    </section>
  )
}
