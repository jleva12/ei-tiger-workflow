import * as React from "react"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldError, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { BuilderContext } from "./schema-builder-context"
import {
  isScalar,
  newProperty,
  SCHEMA_TYPES,
  STRING_FORMATS,
  withType,
  type ConstValue,
  type NodeIssues,
  type SchemaNode,
  type SchemaProperty,
  type SchemaType,
} from "@/lib/json-schema"

const useBuilder = () => React.useContext(BuilderContext)

/** Whether a node, or anything inside it, has a problem. */
function hasIssue(node: SchemaNode, issues: Map<string, NodeIssues>): boolean {
  if (issues.has(node.uid)) return true
  if (node.type === "array" && node.items && hasIssue(node.items, issues)) {
    return true
  }
  return (node.properties ?? []).some((p) => hasIssue(p, issues))
}

/** How a node reads collapsed: `string`, `array<object>`, `integer | null`. */
function typeLabel(node: SchemaNode): string {
  const base =
    node.type === "array" && node.items
      ? `array<${typeLabel(node.items)}>`
      : node.type
  return node.nullable ? `${base} | null` : base
}

/**
 * An object's properties: one row each, in order, with the controls to add,
 * reorder and remove them. The payload's own properties are full rows
 * (`depth` 0); nested ones are compact rows on tree lines.
 */
export function PropertyList({
  properties,
  onChange,
  depth,
  addLabel = "Add property",
}: {
  properties: SchemaProperty[]
  onChange: (properties: SchemaProperty[]) => void
  depth: number
  addLabel?: string
}) {
  const { readOnly } = useBuilder()
  const replace = (index: number, next: SchemaProperty) =>
    onChange(properties.map((p, i) => (i === index ? next : p)))
  const move = (from: number, to: number) => {
    const next = [...properties]
    const [moved] = next.splice(from, 1)
    next.splice(to, 0, moved)
    onChange(next)
  }
  const add = () => onChange([...properties, newProperty()])

  return (
    <div className={cn(depth === 0 ? "flex flex-col gap-1" : "flex flex-col")}>
      {properties.map((property, index) => (
        <PropertyRow
          key={property.uid}
          property={property}
          index={index}
          depth={depth}
          isFirst={index === 0}
          isLast={index === properties.length - 1}
          onChange={(next) => replace(index, next)}
          onRemove={() => onChange(properties.filter((_, i) => i !== index))}
          onMoveUp={() => move(index, index - 1)}
          onMoveDown={() => move(index, index + 1)}
        />
      ))}
      {!readOnly &&
        (depth === 0 ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={add}
            className="mt-2 w-full border-dashed text-muted-foreground"
          >
            <Icon icon="plus" data-icon="inline-start" />
            {addLabel}
          </Button>
        ) : (
          <button
            type="button"
            onClick={add}
            className="ml-6 flex items-center gap-1 rounded-(--radius-item) py-1 text-xs text-muted-foreground hover:text-foreground"
          >
            <Icon icon="plus" size={12} />
            {addLabel.toLowerCase()}
          </button>
        ))}
    </div>
  )
}

function RowActions({
  name,
  isFirst,
  isLast,
  onMoveUp,
  onMoveDown,
  onRemove,
}: {
  name: string
  isFirst: boolean
  isLast: boolean
  onMoveUp: () => void
  onMoveDown: () => void
  onRemove: () => void
}) {
  const { readOnly } = useBuilder()
  if (readOnly) return null
  const label = name || "this property"
  return (
    <div className="flex shrink-0 items-center gap-0.5">
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        className="text-muted-foreground"
        disabled={isFirst}
        onClick={onMoveUp}
        aria-label={`Move ${label} up`}
      >
        <Icon icon="up" />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        className="text-muted-foreground"
        disabled={isLast}
        onClick={onMoveDown}
        aria-label={`Move ${label} down`}
      >
        <Icon icon="down" />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        className="text-muted-foreground hover:text-destructive"
        onClick={onRemove}
        aria-label={`Remove ${label}`}
      >
        <Icon icon="close" />
      </Button>
    </div>
  )
}

/** A property collapsed to one line: `name : type`, its constant, `*` if required. */
function Summary({
  property,
  index,
  compact,
}: {
  property: SchemaProperty
  index: number
  compact: boolean
}) {
  const { issues } = useBuilder()
  const broken = hasIssue(property, issues)
  return (
    <span className="flex min-w-0 items-center gap-2">
      <span
        className={cn(
          "truncate font-mono font-medium",
          compact ? "text-xs" : "text-sm",
          !property.name && "text-subtle",
          broken && "text-destructive"
        )}
      >
        {property.name || `property_${index + 1}`}
      </span>
      <span
        className={cn(
          "shrink-0 font-mono text-muted-foreground",
          compact ? "text-2xs" : "text-xs"
        )}
      >
        : {typeLabel(property)}
      </span>
      {property.format && property.type === "string" && (
        <span className="shrink-0 rounded-(--radius-chip) bg-muted px-1.5 py-px font-mono text-3xs text-muted-foreground">
          {property.format}
        </span>
      )}
      {property.const !== undefined && isScalar(property.type) && (
        <span className="max-w-40 shrink-0 truncate rounded-(--radius-chip) bg-tone-pink px-1.5 py-px font-mono text-3xs text-tone-pink-foreground">
          = {JSON.stringify(property.const)}
        </span>
      )}
      {property.required && (
        <span className="shrink-0 text-xs text-destructive" title="Required">
          *
        </span>
      )}
      {broken && (
        <Icon icon="warning" size={13} className="shrink-0 text-destructive" />
      )}
    </span>
  )
}

function PropertyRow({
  property,
  index,
  depth,
  isFirst,
  isLast,
  onChange,
  onRemove,
  onMoveUp,
  onMoveDown,
}: {
  property: SchemaProperty
  index: number
  depth: number
  isFirst: boolean
  isLast: boolean
  onChange: (property: SchemaProperty) => void
  onRemove: () => void
  onMoveUp: () => void
  onMoveDown: () => void
}) {
  const id = React.useId()
  const { issues, readOnly } = useBuilder()
  // A property just added has no name yet: open it to take one.
  const [expanded, setExpanded] = React.useState(!property.name && !readOnly)
  const compact = depth > 0
  const nodeIssues = issues.get(property.uid)
  const update = (patch: Partial<SchemaProperty>) =>
    onChange({ ...property, ...patch })
  const toggle = (
    <button
      type="button"
      onClick={() => setExpanded((open) => !open)}
      aria-expanded={expanded}
      aria-label={`${expanded ? "Collapse" : "Expand"} ${property.name || "property"}`}
      className="flex size-5 shrink-0 items-center justify-center rounded-(--radius-item) text-muted-foreground hover:bg-accent hover:text-foreground"
    >
      <Icon icon={expanded ? "down" : "right"} size={14} />
    </button>
  )
  const actions = (
    <RowActions
      name={property.name}
      isFirst={isFirst}
      isLast={isLast}
      onMoveUp={onMoveUp}
      onMoveDown={onMoveDown}
      onRemove={onRemove}
    />
  )
  const typeSelect = (
    <TypeSelect
      id={`${id}-type`}
      value={property.type}
      compact={compact}
      onChange={(type) => onChange(withType(property, type))}
    />
  )
  const flags = (
    <div className="flex shrink-0 items-center gap-3">
      <Flag
        id={`${id}-required`}
        label="Required"
        checked={property.required}
        onChange={(required) => update({ required })}
      />
      <Flag
        id={`${id}-nullable`}
        label="Null"
        title="Null is allowed too"
        checked={Boolean(property.nullable)}
        onChange={(nullable) => update({ nullable })}
      />
    </div>
  )

  if (!compact) {
    return (
      <div className="border-b border-border/60 py-1.5 last-of-type:border-b-0">
        <div className="flex items-start gap-2">
          <div className="pt-1.5">{toggle}</div>
          <div className="min-w-0 flex-1">
            {!expanded ? (
              <button
                type="button"
                onClick={() => setExpanded(true)}
                className="flex w-full min-w-0 items-center py-1.5 text-left"
              >
                <Summary property={property} index={index} compact={false} />
              </button>
            ) : (
              <div className="flex flex-col gap-3 pt-1 pb-4">
                <div className="grid grid-cols-[minmax(0,1fr)_9rem_auto] items-start gap-3 @max-[600px]/shell:grid-cols-1">
                  <Field
                    data-invalid={Boolean(nodeIssues?.name)}
                    className="gap-1.5"
                  >
                    <FieldLabel htmlFor={`${id}-name`} className="text-xs">
                      Name
                    </FieldLabel>
                    <Input
                      id={`${id}-name`}
                      value={property.name}
                      placeholder="property_name"
                      autoComplete="off"
                      spellCheck={false}
                      disabled={readOnly}
                      aria-invalid={Boolean(nodeIssues?.name)}
                      onChange={(event) => update({ name: event.target.value })}
                      className="font-mono"
                    />
                    {nodeIssues?.name && (
                      <FieldError>{nodeIssues.name}</FieldError>
                    )}
                  </Field>
                  <Field className="gap-1.5">
                    <FieldLabel htmlFor={`${id}-type`} className="text-xs">
                      Type
                    </FieldLabel>
                    {typeSelect}
                  </Field>
                  <div className="pt-7 @max-[600px]/shell:pt-0">{flags}</div>
                </div>
                <Field className="gap-1.5">
                  <FieldLabel htmlFor={`${id}-description`} className="text-xs">
                    Description
                  </FieldLabel>
                  <Textarea
                    id={`${id}-description`}
                    rows={2}
                    value={property.description ?? ""}
                    placeholder="What this field holds, for whoever sends it"
                    disabled={readOnly}
                    onChange={(event) =>
                      update({ description: event.target.value })
                    }
                  />
                </Field>
                <NodeDetails
                  node={property}
                  onChange={onChange}
                  depth={depth}
                />
              </div>
            )}
          </div>
          <div className="pt-1">{actions}</div>
        </div>
      </div>
    )
  }

  return (
    <div className="relative">
      {/* The tree's line down to this row, and across to it. */}
      <div aria-hidden className="absolute top-0 bottom-0 left-0 flex">
        <div className={cn("w-4 border-l-2", isLast ? "h-3.5" : "h-full")} />
        <div className="h-3.5 w-2 border-b-2" />
      </div>
      <div className="ml-6">
        {!expanded ? (
          <div className="flex items-center gap-1.5 py-0.5">
            {toggle}
            <button
              type="button"
              onClick={() => setExpanded(true)}
              className="flex min-w-0 flex-1 items-center text-left"
            >
              <Summary property={property} index={index} compact />
            </button>
            {actions}
          </div>
        ) : (
          <div className="flex flex-col gap-2 pb-2">
            <div className="flex items-center gap-1.5">
              {toggle}
              <Input
                id={`${id}-name`}
                aria-label="Name"
                value={property.name}
                placeholder="name"
                autoComplete="off"
                spellCheck={false}
                disabled={readOnly}
                aria-invalid={Boolean(nodeIssues?.name)}
                onChange={(event) => update({ name: event.target.value })}
                className="h-7 min-w-0 flex-1 font-mono text-xs md:text-xs"
              />
              {typeSelect}
              {flags}
              {actions}
            </div>
            {nodeIssues?.name && (
              <FieldError className="pl-7 text-xs">
                {nodeIssues.name}
              </FieldError>
            )}
            <div className="flex flex-col gap-2 pl-7">
              <Input
                aria-label="Description"
                value={property.description ?? ""}
                placeholder="description…"
                disabled={readOnly}
                onChange={(event) =>
                  update({ description: event.target.value })
                }
                className="h-7 text-xs md:text-xs"
              />
              <NodeDetails node={property} onChange={onChange} depth={depth} />
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

/**
 * Everything about a node past its name and type: a constant for a scalar,
 * then its type's constraints, items or properties.
 */
function NodeDetails<T extends SchemaNode>({
  node,
  onChange,
  depth,
}: {
  node: T
  onChange: (node: T) => void
  depth: number
}) {
  const compact = depth > 0
  const update = (patch: Partial<SchemaNode>) => onChange({ ...node, ...patch })
  const fixed = node.const !== undefined && isScalar(node.type)
  return (
    <>
      {isScalar(node.type) && (
        // Its own draft text starts over with each type.
        <ConstField
          key={node.type}
          node={node}
          compact={compact}
          onChange={update}
        />
      )}
      {!fixed && node.type === "string" && (
        <Constraints compact={compact}>
          <StringConstraints node={node} compact={compact} onChange={update} />
        </Constraints>
      )}
      {!fixed && (node.type === "number" || node.type === "integer") && (
        <Constraints compact={compact}>
          <NumberConstraints node={node} compact={compact} onChange={update} />
        </Constraints>
      )}
      {node.type === "array" && (
        <Constraints compact={compact}>
          <ArrayConstraints node={node} depth={depth} onChange={update} />
        </Constraints>
      )}
      {node.type === "object" && (
        <ObjectProperties node={node} depth={depth} onChange={update} />
      )}
    </>
  )
}

/** Type-specific settings, set off by a rule on the left at the top level. */
function Constraints({
  compact,
  children,
}: {
  compact: boolean
  children: React.ReactNode
}) {
  return (
    <div
      className={cn(
        "flex flex-col",
        compact ? "gap-2" : "gap-3 border-l-2 border-border/60 pl-4"
      )}
    >
      {children}
    </div>
  )
}

function TypeSelect({
  id,
  value,
  compact,
  onChange,
}: {
  id: string
  value: SchemaType
  compact: boolean
  onChange: (type: SchemaType) => void
}) {
  const { readOnly } = useBuilder()
  return (
    <Select
      items={SCHEMA_TYPES}
      value={value}
      disabled={readOnly}
      onValueChange={(next) => next && onChange(next as SchemaType)}
    >
      <SelectTrigger
        id={id}
        aria-label={compact ? "Type" : undefined}
        size={compact ? "sm" : "default"}
        className={cn("font-mono", compact ? "w-26 text-xs" : "w-full")}
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false}>
        {SCHEMA_TYPES.map((type) => (
          <SelectItem key={type.value} value={type.value} className="font-mono">
            {type.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function Flag({
  id,
  label,
  title,
  checked,
  onChange,
}: {
  id: string
  label: string
  title?: string
  checked: boolean
  onChange: (checked: boolean) => void
}) {
  const { readOnly } = useBuilder()
  return (
    <span className="flex items-center gap-1.5" title={title}>
      <Checkbox
        id={id}
        checked={checked}
        disabled={readOnly}
        onCheckedChange={(on) => onChange(on === true)}
      />
      <label
        htmlFor={id}
        className="text-xs whitespace-nowrap text-muted-foreground select-none"
      >
        {label}
      </label>
    </span>
  )
}

/**
 * A labelled number box at the top level; nested, a bare box whose
 * placeholder names it.
 */
function NumberInput({
  label,
  value,
  onChange,
  compact,
  placeholder,
  integer = false,
  invalid = false,
}: {
  label: string
  value: number | undefined
  onChange: (value: number | undefined) => void
  compact: boolean
  placeholder?: string
  integer?: boolean
  invalid?: boolean
}) {
  const id = React.useId()
  const { readOnly } = useBuilder()
  const input = (
    <Input
      id={id}
      type="number"
      inputMode={integer ? "numeric" : "decimal"}
      min={integer ? 0 : undefined}
      step={integer ? 1 : "any"}
      value={value ?? ""}
      placeholder={compact ? label : placeholder}
      aria-label={compact ? label : undefined}
      aria-invalid={invalid}
      disabled={readOnly}
      onChange={(event) => {
        const next =
          event.target.value === "" ? NaN : Number(event.target.value)
        onChange(Number.isFinite(next) ? next : undefined)
      }}
      className={cn("tabular-nums", compact && "h-7 text-xs md:text-xs")}
    />
  )
  if (compact) return input
  return (
    <Field data-invalid={invalid} className="gap-1.5">
      <FieldLabel htmlFor={id} className="text-xs">
        {label}
      </FieldLabel>
      {input}
    </Field>
  )
}

function Grid({
  compact,
  children,
}: {
  compact: boolean
  children: React.ReactNode
}) {
  return (
    <div
      className={cn(
        "grid grid-cols-3 @max-[600px]/shell:grid-cols-2",
        compact ? "gap-2" : "gap-3"
      )}
    >
      {children}
    </div>
  )
}

function ConstField({
  node,
  compact,
  onChange,
}: {
  node: SchemaNode
  compact: boolean
  onChange: (patch: Partial<SchemaNode>) => void
}) {
  const { subject } = useBuilder()
  const id = React.useId()
  const { readOnly } = useBuilder()
  const [text, setText] = React.useState(
    node.const === undefined ? "" : String(node.const)
  )
  // Numbers are kept as typed until they read as one.
  const numeric = node.type === "number" || node.type === "integer"
  const invalid =
    numeric &&
    text !== "" &&
    (!Number.isFinite(Number(text)) ||
      (node.type === "integer" && !Number.isInteger(Number(text))))
  const set = (value: ConstValue | undefined) => onChange({ const: value })

  const control =
    node.type === "boolean" ? (
      <Select
        items={BOOLEAN_CONSTS}
        value={node.const === undefined ? "none" : String(node.const)}
        disabled={readOnly}
        onValueChange={(value) =>
          set(value === "true" ? true : value === "false" ? false : undefined)
        }
      >
        <SelectTrigger
          id={id}
          size={compact ? "sm" : "default"}
          aria-label={compact ? "Constant value" : undefined}
          className={cn("font-mono", compact ? "w-full text-xs" : "w-40")}
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent alignItemWithTrigger={false}>
          {BOOLEAN_CONSTS.map((item) => (
            <SelectItem key={item.value} value={item.value}>
              {item.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    ) : (
      <Input
        id={id}
        value={text}
        placeholder={compact ? "constant value" : "Any value (leave empty)"}
        aria-label={compact ? "Constant value" : undefined}
        aria-invalid={invalid}
        disabled={readOnly}
        onChange={(event) => {
          const next = event.target.value
          setText(next)
          if (next === "") set(undefined)
          else if (!numeric) set(next)
          else if (Number.isFinite(Number(next))) set(Number(next))
        }}
        className={cn("font-mono", compact && "h-7 text-xs md:text-xs")}
      />
    )

  if (compact) return control
  return (
    <Field data-invalid={invalid} className="gap-1.5">
      <FieldLabel htmlFor={id} className="text-xs">
        <Icon icon="pin" size={12} />
        Constant value
        <span className="font-normal text-subtle">(optional)</span>
      </FieldLabel>
      {control}
      {invalid ? (
        <FieldError>
          {node.type === "integer" ? "Not an integer." : "Not a number."}
        </FieldError>
      ) : (
        <p className="text-xs text-muted-foreground">
          When set, every {subject.one} must carry exactly this value.
        </p>
      )}
    </Field>
  )
}

const BOOLEAN_CONSTS = [
  { value: "none", label: "Any" },
  { value: "true", label: "true" },
  { value: "false", label: "false" },
]

function StringConstraints({
  node,
  compact,
  onChange,
}: {
  node: SchemaNode
  compact: boolean
  onChange: (patch: Partial<SchemaNode>) => void
}) {
  const id = React.useId()
  const { issues, readOnly } = useBuilder()
  const found = issues.get(node.uid)
  const format = (
    <Select
      items={STRING_FORMATS}
      value={node.format ?? "none"}
      disabled={readOnly}
      onValueChange={(value) =>
        onChange({ format: !value || value === "none" ? undefined : value })
      }
    >
      <SelectTrigger
        id={`${id}-format`}
        size={compact ? "sm" : "default"}
        aria-label={compact ? "Format" : undefined}
        className={cn("w-full", compact && "text-xs")}
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false}>
        {STRING_FORMATS.map((item) => (
          <SelectItem key={item.value} value={item.value}>
            {item.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
  const pattern = (
    <Input
      id={`${id}-pattern`}
      value={node.pattern ?? ""}
      placeholder={compact ? "pattern (regex)" : "^INC-[0-9]+$"}
      aria-label={compact ? "Pattern" : undefined}
      aria-invalid={Boolean(found?.pattern)}
      spellCheck={false}
      disabled={readOnly}
      onChange={(event) =>
        onChange({ pattern: event.target.value || undefined })
      }
      className={cn("font-mono", compact && "h-7 text-xs md:text-xs")}
    />
  )
  return (
    <>
      <Grid compact={compact}>
        <NumberInput
          label={compact ? "minLen" : "Min length"}
          placeholder="0"
          integer
          compact={compact}
          value={node.minLength}
          invalid={Boolean(found?.length)}
          onChange={(minLength) => onChange({ minLength })}
        />
        <NumberInput
          label={compact ? "maxLen" : "Max length"}
          placeholder="∞"
          integer
          compact={compact}
          value={node.maxLength}
          invalid={Boolean(found?.length)}
          onChange={(maxLength) => onChange({ maxLength })}
        />
        {compact ? (
          format
        ) : (
          <Field className="gap-1.5">
            <FieldLabel htmlFor={`${id}-format`} className="text-xs">
              Format
            </FieldLabel>
            {format}
          </Field>
        )}
      </Grid>
      {found?.length && (
        <FieldError className="text-xs">{found.length}</FieldError>
      )}
      {compact ? (
        pattern
      ) : (
        <Field data-invalid={Boolean(found?.pattern)} className="gap-1.5">
          <FieldLabel htmlFor={`${id}-pattern`} className="text-xs">
            Pattern (regex)
          </FieldLabel>
          {pattern}
        </Field>
      )}
      {found?.pattern && (
        <FieldError className="text-xs">{found.pattern}</FieldError>
      )}
      <EnumEditor node={node} compact={compact} onChange={onChange} />
    </>
  )
}

function NumberConstraints({
  node,
  compact,
  onChange,
}: {
  node: SchemaNode
  compact: boolean
  onChange: (patch: Partial<SchemaNode>) => void
}) {
  const { issues } = useBuilder()
  const found = issues.get(node.uid)
  const integer = node.type === "integer"
  return (
    <>
      <Grid compact={compact}>
        <NumberInput
          label={compact ? "min" : "Minimum"}
          placeholder="−∞"
          compact={compact}
          value={node.minimum}
          invalid={Boolean(found?.range)}
          onChange={(minimum) => onChange({ minimum })}
        />
        <NumberInput
          label={compact ? "max" : "Maximum"}
          placeholder="∞"
          compact={compact}
          value={node.maximum}
          invalid={Boolean(found?.range)}
          onChange={(maximum) => onChange({ maximum })}
        />
        <NumberInput
          label={compact ? "multipleOf" : "Multiple of"}
          placeholder="—"
          compact={compact}
          value={node.multipleOf}
          invalid={Boolean(found?.multipleOf)}
          onChange={(multipleOf) => onChange({ multipleOf })}
        />
        {!compact && (
          <>
            <NumberInput
              label="Exclusive min"
              placeholder="—"
              compact={false}
              value={node.exclusiveMinimum}
              onChange={(exclusiveMinimum) => onChange({ exclusiveMinimum })}
            />
            <NumberInput
              label="Exclusive max"
              placeholder="—"
              compact={false}
              value={node.exclusiveMaximum}
              onChange={(exclusiveMaximum) => onChange({ exclusiveMaximum })}
            />
          </>
        )}
      </Grid>
      {(found?.range || found?.multipleOf) && (
        <FieldError className="text-xs">
          {found.range ?? found.multipleOf}
        </FieldError>
      )}
      <EnumEditor
        node={node}
        compact={compact}
        onChange={onChange}
        integer={integer}
      />
    </>
  )
}

/** The values a string or number may take, as removable chips. */
function EnumEditor({
  node,
  compact,
  integer = false,
  onChange,
}: {
  node: SchemaNode
  compact: boolean
  integer?: boolean
  onChange: (patch: Partial<SchemaNode>) => void
}) {
  const id = React.useId()
  const { readOnly } = useBuilder()
  const [draft, setDraft] = React.useState("")
  const values = node.enum ?? []
  const numeric = node.type !== "string"
  const parsed: string | number | undefined = !draft.trim()
    ? undefined
    : numeric
      ? Number.isFinite(Number(draft)) &&
        (!integer || Number.isInteger(Number(draft)))
        ? Number(draft)
        : undefined
      : draft.trim()
  const add = () => {
    if (parsed === undefined || values.includes(parsed)) return
    onChange({ enum: [...values, parsed] })
    setDraft("")
  }
  const remove = (value: string | number) => {
    const next = values.filter((v) => v !== value)
    onChange({ enum: next.length ? next : undefined })
  }

  const entry = !readOnly && (
    <div className="flex gap-1.5">
      <Input
        id={id}
        value={draft}
        placeholder={
          compact ? "allowed value…" : "Add an allowed value, then Enter"
        }
        aria-label={compact ? "Allowed value" : undefined}
        inputMode={numeric ? "decimal" : undefined}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault()
            add()
          }
        }}
        className={cn(compact && "h-7 text-xs md:text-xs")}
      />
      <Button
        type="button"
        variant="outline"
        size={compact ? "icon-xs" : "icon"}
        className={compact ? "size-7" : undefined}
        disabled={parsed === undefined || values.includes(parsed)}
        onClick={add}
        aria-label="Add allowed value"
      >
        <Icon icon="plus" />
      </Button>
    </div>
  )
  const chips = values.length > 0 && (
    <ul className="flex flex-wrap gap-1" aria-label="Allowed values">
      {values.map((value) => (
        <li
          key={String(value)}
          className="inline-flex items-center gap-1 rounded-(--radius-chip) bg-muted py-0.5 pr-1 pl-2 font-mono text-2xs"
        >
          {typeof value === "string" ? value : String(value)}
          {!readOnly && (
            <button
              type="button"
              onClick={() => remove(value)}
              aria-label={`Remove ${value}`}
              className="rounded-(--radius-chip) text-muted-foreground hover:text-destructive"
            >
              <Icon icon="close" size={11} />
            </button>
          )}
        </li>
      ))}
    </ul>
  )
  if (readOnly && !values.length) return null
  if (compact) {
    return (
      <div className="flex flex-col gap-1.5">
        {entry}
        {chips}
      </div>
    )
  }
  return (
    <Field className="gap-1.5">
      <FieldLabel htmlFor={id} className="text-xs">
        Allowed values
        <span className="font-normal text-subtle">(optional)</span>
      </FieldLabel>
      {entry}
      {chips}
    </Field>
  )
}

function ArrayConstraints({
  node,
  depth,
  onChange,
}: {
  node: SchemaNode
  depth: number
  onChange: (patch: Partial<SchemaNode>) => void
}) {
  const id = React.useId()
  const compact = depth > 0
  const { issues } = useBuilder()
  const found = issues.get(node.uid)
  const items = node.items ?? { uid: `${node.uid}-items`, type: "string" }
  const unique = (
    <Flag
      id={`${id}-unique`}
      label="Unique items"
      checked={Boolean(node.uniqueItems)}
      onChange={(uniqueItems) => onChange({ uniqueItems })}
    />
  )
  return (
    <>
      <Grid compact={compact}>
        <NumberInput
          label={compact ? "minItems" : "Min items"}
          placeholder="0"
          integer
          compact={compact}
          value={node.minItems}
          invalid={Boolean(found?.items)}
          onChange={(minItems) => onChange({ minItems })}
        />
        <NumberInput
          label={compact ? "maxItems" : "Max items"}
          placeholder="∞"
          integer
          compact={compact}
          value={node.maxItems}
          invalid={Boolean(found?.items)}
          onChange={(maxItems) => onChange({ maxItems })}
        />
        <div className={cn("flex items-center", !compact && "pt-6")}>
          {unique}
        </div>
      </Grid>
      {found?.items && (
        <FieldError className="text-xs">{found.items}</FieldError>
      )}
      <ItemsEditor
        items={items}
        depth={depth}
        onChange={(next) => onChange({ items: next })}
      />
    </>
  )
}

/**
 * An array's items: their type, and then whatever that type takes, down
 * to arrays of arrays.
 */
function ItemsEditor({
  items,
  depth,
  onChange,
}: {
  items: SchemaNode
  depth: number
  onChange: (items: SchemaNode) => void
}) {
  const id = React.useId()
  const update = (patch: Partial<SchemaNode>) =>
    onChange({ ...items, ...patch })
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor={`${id}-type`} className="text-xs text-muted-foreground">
          Each item is
        </label>
        <TypeSelect
          id={`${id}-type`}
          value={items.type}
          compact
          onChange={(type) => onChange(withType(items, type))}
        />
        <Flag
          id={`${id}-nullable`}
          label="Null"
          title="Items may be null too"
          checked={Boolean(items.nullable)}
          onChange={(nullable) => update({ nullable })}
        />
      </div>
      {(items.type !== "boolean" || items.const !== undefined) && (
        <div className="pl-3">
          <NodeDetails node={items} onChange={onChange} depth={depth + 1} />
        </div>
      )}
    </div>
  )
}

/**
 * An object's properties as a tree under it, and whether it takes
 * properties it doesn't list.
 */
function ObjectProperties({
  node,
  depth,
  onChange,
}: {
  node: SchemaNode
  depth: number
  onChange: (patch: Partial<SchemaNode>) => void
}) {
  const id = React.useId()
  return (
    <div
      className={cn(
        "flex flex-col gap-1",
        depth === 0 && "border-l-2 border-border/60 pl-4"
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs text-muted-foreground">Properties</span>
        <Flag
          id={`${id}-closed`}
          label="Only these"
          title="Refuse properties this object doesn't list"
          checked={node.additionalProperties === false}
          onChange={(closed) =>
            onChange({ additionalProperties: closed ? false : undefined })
          }
        />
      </div>
      <PropertyList
        properties={node.properties ?? []}
        depth={depth + 1}
        onChange={(properties) => onChange({ properties })}
      />
    </div>
  )
}
