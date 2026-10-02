import * as React from "react"

import { Checkbox } from "@/components/ui/checkbox"
import {
  Field,
  FieldContent,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import type { SchemaProperty } from "@/lib/json-schema"
import {
  isGroup,
  isJson,
  named,
  pathOf,
  type SchemaErrors,
  type SchemaRaw,
  type SchemaValue,
  type SchemaValueMode,
} from "./schema-value"

/** A schema's fields, nested objects as groups of their own. */
function SchemaFields({
  properties,
  raw,
  errors,
  onChange,
  disabled,
  parent = "",
}: {
  properties: SchemaProperty[]
  raw: SchemaRaw
  errors: SchemaErrors
  onChange: (path: string, value: string | boolean) => void
  disabled: boolean
  parent?: string
}) {
  const id = React.useId()
  return (
    <FieldGroup className="gap-4">
      {named(properties).map((property) => {
        const path = pathOf(parent, property.name)
        const fieldId = `${id}-${path}`
        const error = errors[path]
        const label = (
          <>
            {property.name}
            {property.required && (
              <span aria-hidden className="text-destructive">
                *
              </span>
            )}
          </>
        )
        if (isGroup(property)) {
          return (
            <FieldSet
              key={path}
              className="gap-4 rounded-(--radius-card) border border-border p-3.5"
            >
              <FieldLegend variant="label" className="mb-0 font-mono text-xs">
                {label}
              </FieldLegend>
              {property.description && (
                <FieldDescription className="-mt-2">
                  {property.description}
                </FieldDescription>
              )}
              <SchemaFields
                properties={property.properties ?? []}
                raw={raw}
                errors={errors}
                onChange={onChange}
                disabled={disabled}
                parent={path}
              />
            </FieldSet>
          )
        }
        if (property.type === "boolean") {
          return (
            <Field key={path} orientation="horizontal">
              <Checkbox
                id={fieldId}
                checked={raw[path] === true}
                disabled={disabled}
                onCheckedChange={(checked) => onChange(path, checked === true)}
              />
              <FieldContent>
                <FieldLabel htmlFor={fieldId} className="font-mono text-xs">
                  {label}
                </FieldLabel>
                {property.description && (
                  <FieldDescription>{property.description}</FieldDescription>
                )}
              </FieldContent>
            </Field>
          )
        }
        const text = typeof raw[path] === "string" ? (raw[path] as string) : ""
        const choices = property.enum?.map(String)
        return (
          <Field key={path} data-invalid={Boolean(error)}>
            <FieldLabel htmlFor={fieldId} className="font-mono text-xs">
              {label}
            </FieldLabel>
            {choices?.length ? (
              <Select
                items={choices.map((value) => ({ value, label: value }))}
                value={text || null}
                onValueChange={(value) => onChange(path, value ?? "")}
                disabled={disabled}
              >
                <SelectTrigger
                  id={fieldId}
                  className="w-full"
                  aria-invalid={Boolean(error)}
                >
                  <SelectValue placeholder="Choose…" />
                </SelectTrigger>
                <SelectContent alignItemWithTrigger={false}>
                  {choices.map((value) => (
                    <SelectItem key={value} value={value}>
                      {value}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : isJson(property) ? (
              <Textarea
                id={fieldId}
                rows={3}
                spellCheck={false}
                value={text}
                disabled={disabled}
                aria-invalid={Boolean(error)}
                placeholder={property.type === "array" ? "[]" : "{}"}
                onChange={(event) => onChange(path, event.target.value)}
                className="font-mono text-xs md:text-xs"
              />
            ) : (
              <Input
                id={fieldId}
                value={text}
                disabled={disabled}
                inputMode={
                  property.type === "integer"
                    ? "numeric"
                    : property.type === "number"
                      ? "decimal"
                      : undefined
                }
                aria-invalid={Boolean(error)}
                onChange={(event) => onChange(path, event.target.value)}
              />
            )}
            {property.description && (
              <FieldDescription>{property.description}</FieldDescription>
            )}
            {error && <FieldError>{error}</FieldError>}
          </Field>
        )
      })}
    </FieldGroup>
  )
}

/**
 * A value filled in against its schema (`useSchemaValue`): a heading with
 * Fields / JSON to switch between them when the schema declares fields,
 * then the fields or the JSON. Its parts are siblings, for the caller's
 * column to space.
 */
export function SchemaValueEditor({
  value,
  title,
  toggleLabel,
  disabled,
  jsonPlaceholder,
  noFields,
}: {
  value: SchemaValue
  title: React.ReactNode
  /** Names Fields / JSON for assistive tech, e.g. "Edit the input as". */
  toggleLabel: string
  disabled: boolean
  /** The JSON's placeholder when the schema declares no fields. */
  jsonPlaceholder: string
  /** Under the JSON, when the schema declares no fields. */
  noFields: React.ReactNode
}) {
  const id = React.useId()
  return (
    <>
      <div className="flex items-center justify-between gap-3">
        <h3 id={`${id}-title`} className="text-sm font-medium">
          {title}
        </h3>
        {value.hasFields && (
          <ToggleGroup
            aria-label={toggleLabel}
            value={[value.mode]}
            onValueChange={(next) => {
              if (next[0]) value.switchTo(next[0] as SchemaValueMode)
            }}
            spacing={0}
            className="rounded-(--radius-control) bg-muted p-0.5"
          >
            {(["form", "json"] as const).map((key) => (
              <ToggleGroupItem
                key={key}
                value={key}
                disabled={disabled}
                className="h-[22px] min-w-0 rounded-(--radius-soft)! px-2 text-2xs font-normal text-muted-foreground hover:bg-transparent hover:text-foreground aria-pressed:bg-background aria-pressed:text-foreground aria-pressed:shadow-(--shadow-raised)"
              >
                {key === "form" ? "Fields" : "JSON"}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        )}
      </div>
      {value.mode === "form" ? (
        <SchemaFields
          properties={value.properties}
          raw={value.raw}
          errors={value.errors}
          disabled={disabled}
          onChange={value.setField}
        />
      ) : (
        <Field data-invalid={Boolean(value.jsonError)}>
          <Textarea
            aria-labelledby={`${id}-title`}
            rows={10}
            spellCheck={false}
            value={value.text}
            disabled={disabled}
            aria-invalid={Boolean(value.jsonError)}
            placeholder={value.hasFields ? "{}" : jsonPlaceholder}
            onChange={(event) => value.editText(event.target.value)}
            className="min-h-48 font-mono text-xs md:text-xs"
          />
          {!value.hasFields && <FieldDescription>{noFields}</FieldDescription>}
          {value.jsonError && <FieldError>{value.jsonError}</FieldError>}
        </Field>
      )}
    </>
  )
}
