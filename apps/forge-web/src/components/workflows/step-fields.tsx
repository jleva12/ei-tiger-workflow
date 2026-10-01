import * as React from "react"
import { Delete02Icon } from "@hugeicons/core-free-icons"
import { cn } from "cn"

import {
  bare,
  ChoiceField,
  CODE,
  ListEditor,
  NumberField,
  Row,
  TextField,
} from "@/components/builder/fields/basic-fields"
import { ModelFields } from "@/components/builder/fields/model-fields"
import { SchemaField } from "@/components/builder/fields/schema-field"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  DELAY_UNITS,
  HTTP_METHODS,
  uid,
  type HeaderRow,
  type StepConfigs,
  type StepData,
  type StepKind,
} from "@/lib/workflows/model"
import { resolveExpression } from "@/lib/workflows/expressions"
import { expressionSetting } from "@/lib/workflows/fields"
import { oneOf, valuesOf } from "@/lib/workflows/types"
import { useOrganizationWorkflows } from "@/lib/workflows/api"
import { useBuilder } from "./builder-store"
import { IssueMessages } from "@/components/builder/fields/field-issues"
import { useFieldIssues } from "@/components/builder/fields/field-issues-context"
import { ExpressionField } from "./expression-field"

/*
 * The settings of each kind of step, as its settings dialog shows them. Every
 * change goes to the builder's store as it's typed; typing in one field is
 * one step of undo.
 */

type Setter<K extends StepKind> = <F extends keyof StepConfigs[K]>(
  field: F,
  value: StepConfigs[K][F]
) => void

function useSetter<K extends StepKind>(id: string): Setter<K> {
  const updateStep = useBuilder((s) => s.updateStep)
  return React.useCallback(
    (field, value) =>
      updateStep(
        id,
        (step) =>
          ({ ...step, config: { ...step.config, [field]: value } }) as StepData,
        String(field)
      ),
    [id, updateStep]
  )
}

type Props<K extends StepKind> = { id: string; config: StepConfigs[K] }

/**
 * A setting that reads the run's data, completed and checked against what
 * the step can read. How it's written (an expression, a template, code,
 * arguments) and what it must come to come from the step's kind
 * (lib/workflows/fields.ts), the same as the workflow's issues.
 */
function Setting({
  id,
  setting,
  ...field
}: Omit<
  React.ComponentProps<typeof ExpressionField>,
  "mode" | "scope" | "check"
> & {
  id: string
  /** Its key in lib/workflows/fields.ts. */
  setting: string
}) {
  const scopeOf = useBuilder((s) => s.scopeOf)
  const step = useBuilder((s) => s.nodes.find((n) => n.id === id)?.data)
  const scope = React.useMemo(() => scopeOf(id), [scopeOf, id])
  const spec = React.useMemo(
    () => (step ? expressionSetting(step, setting) : undefined),
    [step, setting]
  )
  // Its issues, but for its expression's: it checks those itself.
  const { issues, key } = useFieldIssues(setting, { whole: true })
  if (!spec) return null
  return (
    <ExpressionField
      {...field}
      mode={spec.mode}
      scope={scope}
      check={spec.check}
      issues={issues.filter((i) => !i.id.includes(":field:"))}
      field={key}
    />
  )
}

/**
 * An HTTP request's headers: a row each, its name and its value side by
 * side and a bin to remove it. The value is a template, completed and
 * checked like the URL.
 */
function HeaderList({
  id,
  headers,
  onChange,
}: {
  id: string
  headers: HeaderRow[]
  onChange: (headers: HeaderRow[]) => void
}) {
  // A header just added: its name takes focus.
  const [added, setAdded] = React.useState<string>()
  // The name and the bin stand as tall as the value's editor.
  const tall = "h-[2.125rem]"
  const change = (header: HeaderRow, patch: Partial<HeaderRow>) =>
    onChange(headers.map((x) => (x.id === header.id ? { ...x, ...patch } : x)))
  // "Every header needs a name": the ones without turn red.
  const { issues, invalid, key } = useFieldIssues("headers")
  return (
    <div
      role="group"
      aria-label="Headers"
      data-field={key}
      className="flex flex-col gap-2"
    >
      <span className="text-xs font-medium text-foreground">Headers</span>
      {headers.map((header) => (
        <div
          key={header.id}
          className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)_auto] items-start gap-1.5"
        >
          <Input
            aria-label="Header name"
            value={header.name}
            placeholder="Key"
            spellCheck={false}
            autoComplete="off"
            autoFocus={header.id === added}
            aria-invalid={(invalid && !header.name.trim()) || undefined}
            onChange={(event) => change(header, { name: event.target.value })}
            className={cn(CODE, tall)}
          />
          <Setting
            id={id}
            setting={`headers.${header.id}`}
            label={`${header.name.trim() || "Header"} value`}
            labelHidden
            value={header.value}
            placeholder="Value"
            onChange={(value) => change(header, { value })}
            // No space under it until it has something to say.
            className="gap-0 has-[[aria-live]>*]:gap-1.5"
          />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={`Remove ${header.name.trim() || "header"}`}
            onClick={() => onChange(headers.filter((x) => x.id !== header.id))}
            className={cn(tall, "text-muted-foreground hover:text-destructive")}
          >
            <Icon icon={Delete02Icon} />
          </Button>
        </div>
      ))}
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => {
          const header = { id: uid("header"), name: "", value: "" }
          setAdded(header.id)
          onChange([...headers, header])
        }}
        className="w-full border-dashed text-muted-foreground"
      >
        <Icon icon="plus" data-icon="inline-start" />
        Add header
      </Button>
      <IssueMessages issues={issues} />
    </div>
  )
}

/* -------------------------------------------------------------------------- */
/* Kinds                                                                      */
/* -------------------------------------------------------------------------- */

/**
 * The start step: the values every run starts with, as a JSON Schema built
 * in a sheet with the same builder as an event type's payload (and an
 * agent's answer). What starts the workflow is hooked up elsewhere, and
 * must send these; every step reads them as `input`, completed and checked
 * against them.
 */
function EntryFields({ id, config }: Props<"entry">) {
  const set = useSetter<"entry">(id)
  return (
    <SchemaField
      issue="input_schema"
      label="Input fields"
      title="The workflow's input"
      description={
        <>
          The values every run starts with: each field, its type and whether
          it&apos;s required. Whatever starts the workflow must send them, and
          every step reads them as{" "}
          <code className="font-mono text-[0.9em]">input</code>.
        </>
      }
      schema={config.input_schema}
      onChange={(schema) => set("input_schema", schema)}
      empty="Not declared: a run can start with anything, and what steps read from input isn't checked."
      subject={{ one: "input", many: "inputs" }}
    />
  )
}

function AgentFields({ id, config }: Props<"agent">) {
  const set = useSetter<"agent">(id)
  const updateStep = useBuilder((s) => s.updateStep)
  return (
    <>
      <Setting
        id={id}
        setting="instructions"
        label="Instructions"
        value={config.instructions}
        multiline
        rows={5}
        placeholder="What the agent should do, and what to hand on."
        onChange={(value) => set("instructions", value)}
        description={
          <>
            Type <code className="font-mono text-[0.9em]">{"{{"}</code> to put
            in the run&apos;s data: the input, or what an earlier step handed
            on.
          </>
        }
      />
      <ModelFields
        model={config.model}
        thinkingLevel={config.thinking_level}
        onChange={(patch, key) =>
          updateStep(
            id,
            (step) =>
              ({ ...step, config: { ...step.config, ...patch } }) as StepData,
            key
          )
        }
      />
      <ChoiceField
        issue="output"
        label="It returns"
        value={config.output}
        options={[
          { value: "text", label: "Text" },
          { value: "json", label: "JSON, matching a schema" },
        ]}
        onChange={(value) => set("output", value)}
      />
      {config.output === "json" && (
        <OutputSchema id={id} text={config.output_schema} />
      )}
    </>
  )
}

/**
 * The JSON an agent returns, declared field by field. Kept as JSON text in
 * the workflow; text that isn't JSON can be started over.
 */
function OutputSchema({ id, text }: { id: string; text: string }) {
  const set = useSetter<"agent">(id)
  let schema: Record<string, unknown> | undefined
  let broken = false
  try {
    const value: unknown = text.trim() ? JSON.parse(text) : undefined
    if (value && typeof value === "object" && !Array.isArray(value)) {
      schema = value as Record<string, unknown>
    } else if (value !== undefined) broken = true
  } catch {
    broken = true
  }
  if (broken) {
    return (
      <div className="flex flex-col gap-2">
        <span className="text-xs font-medium text-foreground">
          What it returns
        </span>
        <p className="flex items-start gap-1.5 text-xs/[1.5] text-destructive">
          <Icon icon="failed" size={13} className="mt-0.5 shrink-0" />
          Its schema isn&apos;t a JSON Schema object, so later steps can&apos;t
          be checked against it.
        </p>
        <Button
          type="button"
          variant="outline"
          size="xs"
          className="self-start"
          onClick={() => set("output_schema", "")}
        >
          Start it over
        </Button>
      </div>
    )
  }
  return (
    <SchemaField
      issue="output_schema"
      label="What it returns"
      title="The JSON the agent returns"
      description={
        <>
          Its fields and their types. The agent is held to them, and later steps
          read them as{" "}
          <code className="font-mono text-[0.9em]">steps.{id}.output</code>,
          completed and checked.
        </>
      }
      schema={schema}
      onChange={(next) =>
        set(
          "output_schema",
          Object.keys(next).length ? JSON.stringify(next, null, 2) : ""
        )
      }
      empty="Not declared: later steps can't be checked against what it returns."
      subject={{ one: "answer", many: "answers" }}
    />
  )
}

function ApprovalFields({ id, config }: Props<"approval">) {
  const set = useSetter<"approval">(id)
  return (
    <>
      <Setting
        id={id}
        setting="message"
        label="What they're deciding"
        value={config.message}
        multiline
        rows={3}
        placeholder="Approve {{ input.request.item }} for {{ input.request.amount }}?"
        onChange={(value) => set("message", value)}
      />
      <ChoiceField
        issue="approvers"
        label="Who decides"
        value={config.approvers}
        options={[
          { value: "org:admin", label: "An organization admin" },
          { value: "org:member", label: "Any member of the organization" },
        ]}
        onChange={(value) => set("approvers", value)}
      />
      <NumberField
        issue="timeout_hours"
        label="Wait up to (hours)"
        value={config.timeout_hours}
        onChange={(value) => set("timeout_hours", value)}
        description="0 waits until someone decides. When it runs out, the step takes Rejected."
      />
    </>
  )
}

function HttpFields({ id, config }: Props<"http">) {
  const set = useSetter<"http">(id)
  return (
    <>
      <div className="grid grid-cols-[5.5rem_1fr] gap-2.5">
        <ChoiceField
          issue="method"
          label="Method"
          value={config.method}
          options={HTTP_METHODS.map((m) => ({ value: m, label: m }))}
          onChange={(value) => set("method", value)}
        />
        <Setting
          id={id}
          setting="url"
          label="URL"
          value={config.url}
          placeholder="https://"
          onChange={(value) => set("url", value)}
        />
      </div>
      <HeaderList
        id={id}
        headers={config.headers}
        onChange={(value) => set("headers", value)}
      />
      {config.method !== "GET" && (
        <Setting
          id={id}
          setting="body"
          label="Body"
          value={config.body}
          multiline
          rows={4}
          placeholder={'{ "key": input.issue.key }'}
          onChange={(value) => set("body", value)}
          description="JSONata. An object or list is sent as JSON; text as it is."
        />
      )}
      <Row>
        <NumberField
          issue="timeout_seconds"
          label="Timeout (s)"
          value={config.timeout_seconds}
          min={1}
          onChange={(value) => set("timeout_seconds", value)}
        />
        <NumberField
          issue="retries"
          label="Retries"
          value={config.retries}
          onChange={(value) => set("retries", value)}
        />
      </Row>
      <SchemaField
        issue="output_schema"
        label="What it returns"
        title="The response's body"
        description={
          <>
            Its fields and their types. Later steps read them as{" "}
            <code className="font-mono text-[0.9em]">
              steps.{id}.output.body
            </code>
            , completed and checked, and a response that doesn&apos;t fit takes
            Error.
          </>
        }
        schema={config.output_schema}
        onChange={(schema) => set("output_schema", schema)}
        empty="Not declared: later steps can't be checked against the response's body."
        subject={{ one: "response", many: "responses" }}
      />
      <p className="text-xs/[1.6] text-muted-foreground">
        {Object.keys(config.output_schema).length
          ? "A 2xx response that fits what it returns takes Success; anything else, or no answer, takes Error."
          : "A 2xx response takes Success; anything else, or no answer, takes Error."}
      </p>
    </>
  )
}

function TransformFields({ id, config }: Props<"transform">) {
  const set = useSetter<"transform">(id)
  return (
    <>
      <Setting
        id={id}
        setting="expression"
        label="Expression"
        value={config.expression}
        multiline
        rows={8}
        placeholder={
          '{\n  "key": input.issue.key,\n  "labels": previous.labels.name\n}'
        }
        onChange={(value) => set("expression", value)}
        description={
          <>
            JSONata. It reads{" "}
            <code className="font-mono text-[0.9em]">input</code>,{" "}
            <code className="font-mono text-[0.9em]">previous</code> and{" "}
            <code className="font-mono text-[0.9em]">steps</code>; what it makes
            is what the next step gets.
          </>
        }
      />
      <SchemaField
        issue="output_schema"
        label="What it makes"
        title="What the transform makes"
        description={
          <>
            Its fields and their types. Later steps read them as{" "}
            <code className="font-mono text-[0.9em]">steps.{id}.output</code>,
            completed and checked, and a run is held to them. Undeclared,
            they&apos;re worked out from the expression where they can be.
          </>
        }
        schema={config.output_schema}
        onChange={(schema) => set("output_schema", schema)}
        empty="Not declared: worked out from the expression where it can be."
        subject={{ one: "result", many: "results" }}
      />
    </>
  )
}

function DelayFields({ id, config }: Props<"delay">) {
  const set = useSetter<"delay">(id)
  return (
    <Row>
      <NumberField
        issue="amount"
        label="Wait"
        value={config.amount}
        min={0}
        onChange={(value) => set("amount", value)}
      />
      <ChoiceField
        issue="unit"
        label="Unit"
        value={config.unit}
        options={DELAY_UNITS}
        onChange={(value) => set("unit", value)}
      />
    </Row>
  )
}

function SubworkflowFields({ id, config }: Props<"subworkflow">) {
  const set = useSetter<"subworkflow">(id)
  const { organization_id: organizationId, id: selfId } = useBuilder(
    (s) => s.meta
  )
  const { workflows } = useOrganizationWorkflows(organizationId)
  const others = workflows.filter((w) => w.id !== selfId)
  const waitId = React.useId()
  return (
    <>
      <ChoiceField
        issue="workflow"
        label="Workflow"
        value={config.workflow}
        placeholder={
          others.length ? "Pick a workflow" : "No other workflows yet"
        }
        options={others.map((w) => ({ value: w.id, label: w.name }))}
        onChange={(value) => set("workflow", value)}
        description="Its start step gets this step's input."
      />
      <Field orientation="horizontal">
        <Checkbox
          id={waitId}
          checked={config.wait}
          onCheckedChange={(on) => set("wait", Boolean(on))}
        />
        <FieldLabel htmlFor={waitId} className="font-normal">
          Wait for it to finish, and hand on its result
        </FieldLabel>
      </Field>
    </>
  )
}

function IfFields({ id, config }: Props<"if">) {
  const set = useSetter<"if">(id)
  return (
    <Setting
      id={id}
      setting="condition"
      label="Condition"
      value={config.condition}
      multiline
      placeholder={'{{ previous.priority }} = "high"'}
      onChange={(value) => set("condition", value)}
      description="Type {{ to pick a field. Compare with =, !=, > or <; combine conditions with and / or. True takes the True way; anything else, False."
    />
  )
}

/**
 * A case's value, picked from the only values the Switch's value can
 * have. One another case has is taken; one it can't have (the field's
 * values changed) shows as such, and the checks flag it.
 */
function CaseChoice({
  label,
  value,
  values,
  taken,
  onChange,
}: {
  label: string
  value: string
  values: string[]
  taken: string[]
  onChange: (value: string) => void
}) {
  const stray = value && !values.includes(value)
  const options = [
    ...values.map((v) => ({ value: v, label: v })),
    ...(stray ? [{ value, label: `${value} (it can't be this)` }] : []),
  ]
  return (
    <Select
      items={options}
      value={value || null}
      onValueChange={(next) => {
        if (typeof next === "string") onChange(next)
      }}
    >
      <SelectTrigger
        aria-label={label}
        size="sm"
        className={cn(bare, CODE, "w-full", stray && "text-destructive")}
      >
        <SelectValue placeholder="Pick a value" />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false}>
        {options.map((option) => (
          <SelectItem
            key={option.value}
            value={option.value}
            disabled={option.value !== value && taken.includes(option.value)}
            className={cn(
              CODE,
              option.value === value && stray && "text-destructive"
            )}
          >
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function SwitchFields({ id, config }: Props<"switch">) {
  const set = useSetter<"switch">(id)
  const scopeOf = useBuilder((s) => s.scopeOf)
  // The only values it can switch on, when what it's on says so: then a
  // case is picked from them, and can't be one it could never be.
  const values = React.useMemo(
    () =>
      config.value.trim()
        ? valuesOf(resolveExpression(config.value, scopeOf(id)))
        : undefined,
    [config.value, scopeOf, id]
  )
  const used = config.cases.map((c) => c.value)
  const open = values?.filter((v) => !used.includes(v)) ?? []
  return (
    <>
      <Setting
        id={id}
        setting="value"
        label="Switch on"
        value={config.value}
        placeholder="previous.kind"
        onChange={(value) => set("value", value)}
        description="JSONata. The case it equals, as text, is taken."
      />
      <ListEditor
        issue="cases"
        label="Cases"
        items={config.cases}
        onChange={(value) => set("cases", value)}
        make={() => ({ id: uid("case"), value: open[0] ?? "" })}
        addLabel="Add case"
        addDisabled={values !== undefined && open.length === 0}
        renderItem={(item, change, index) => ({
          head: values ? (
            <CaseChoice
              label={`Case ${index + 1}`}
              value={item.value}
              values={values}
              taken={config.cases
                .filter((c) => c.id !== item.id)
                .map((c) => c.value)}
              onChange={(value) => change({ value })}
            />
          ) : (
            <Input
              aria-label={`Case ${index + 1}`}
              value={item.value}
              placeholder="bug"
              onChange={(event) => change({ value: event.target.value })}
              className={cn(bare, CODE)}
            />
          ),
        })}
        description={
          values ? (
            open.length ? (
              <>
                It&apos;s {oneOf(values)}: each case is one of them. A value no
                case has ({oneOf(open)}) takes Default.
              </>
            ) : (
              <>
                It&apos;s {oneOf(values)}, and every one has a case: Default is
                never taken.
              </>
            )
          ) : (
            <>
              A value no case has takes Default. Declare the values it can have
              where it&apos;s made (a field with a fixed list of values), and
              each case is picked from them.
            </>
          )
        }
      />
    </>
  )
}

function MatchFields({ id, config }: Props<"match">) {
  const set = useSetter<"match">(id)
  return (
    <ListEditor
      issue="arms"
      label="Rules, in order"
      items={config.arms}
      onChange={(value) => set("arms", value)}
      make={() => ({ id: uid("rule"), label: "", condition: "" })}
      addLabel="Add rule"
      ordered
      renderItem={(arm, change, index) => ({
        head: (
          <Input
            aria-label={`Rule ${index + 1} name`}
            value={arm.label}
            placeholder={`Rule ${index + 1}`}
            onChange={(event) => change({ label: event.target.value })}
            className={cn(bare, "text-xs font-medium md:text-xs")}
          />
        ),
        body: (
          <Setting
            id={id}
            setting={`arms.${arm.id}`}
            label={`${arm.label.trim() || `Rule ${index + 1}`} condition`}
            labelHidden
            value={arm.condition}
            multiline
            placeholder={'previous.severity = "high"'}
            onChange={(value) => change({ condition: value })}
          />
        ),
      })}
      description="Each condition is JSONata. The first that's true is taken; none takes Otherwise."
    />
  )
}

function LoopFields({ id, config }: Props<"loop">) {
  const set = useSetter<"loop">(id)
  return (
    <>
      <Setting
        id={id}
        setting="items"
        label="Go through"
        value={config.items}
        placeholder="previous.items"
        onChange={(value) => set("items", value)}
        description="JSONata giving a list. Each item's fields complete in the body."
      />
      <TextField
        issue="item_name"
        label="Call each"
        value={config.item_name}
        code
        onChange={(value) => set("item_name", value)}
        description={
          <>
            The body reads the current one as{" "}
            <code className="font-mono text-[0.9em]">
              {config.item_name || "item"}
            </code>
            .
          </>
        }
      />
      <Row>
        <NumberField
          issue="max_iterations"
          label="Most passes"
          value={config.max_iterations}
          min={1}
          onChange={(value) => set("max_iterations", value)}
        />
        <NumberField
          issue="concurrency"
          label="At once"
          value={config.concurrency}
          min={1}
          onChange={(value) => set("concurrency", value)}
        />
      </Row>
      <p className="text-xs/[1.6] text-muted-foreground">
        Connect Each item to the steps to repeat, and the last of them back to
        the loop. Done goes on when every item has run.
      </p>
    </>
  )
}

function MergeFields({ id, config }: Props<"merge">) {
  const set = useSetter<"merge">(id)
  return (
    <ChoiceField
      issue="mode"
      label="Goes on"
      value={config.mode}
      options={[
        { value: "all", label: "When every way has arrived" },
        { value: "any", label: "At the first way to arrive" },
      ]}
      onChange={(value) => set("mode", value)}
    />
  )
}

function EndFields({ id, config }: Props<"end">) {
  const set = useSetter<"end">(id)
  return (
    <>
      <ChoiceField
        issue="outcome"
        label="The run"
        value={config.outcome}
        options={[
          { value: "succeeded", label: "Succeeds" },
          { value: "failed", label: "Fails" },
        ]}
        onChange={(value) => set("outcome", value)}
      />
      <Setting
        id={id}
        setting="result"
        label="Result"
        value={config.result}
        placeholder="previous"
        onChange={(value) => set("result", value)}
        description="JSONata: what the run hands back."
      />
    </>
  )
}

/** The settings of a step, for its kind. */
export function StepFields({ id, step }: { id: string; step: StepData }) {
  switch (step.kind) {
    case "entry":
      return <EntryFields id={id} config={step.config} />
    case "agent":
      return <AgentFields id={id} config={step.config} />
    case "approval":
      return <ApprovalFields id={id} config={step.config} />
    case "http":
      return <HttpFields id={id} config={step.config} />
    case "transform":
      return <TransformFields id={id} config={step.config} />
    case "delay":
      return <DelayFields id={id} config={step.config} />
    case "subworkflow":
      return <SubworkflowFields id={id} config={step.config} />
    case "if":
      return <IfFields id={id} config={step.config} />
    case "switch":
      return <SwitchFields id={id} config={step.config} />
    case "match":
      return <MatchFields id={id} config={step.config} />
    case "loop":
      return <LoopFields id={id} config={step.config} />
    case "merge":
      return <MergeFields id={id} config={step.config} />
    case "end":
      return <EndFields id={id} config={step.config} />
  }
}
