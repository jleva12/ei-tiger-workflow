import * as React from "react"
import { cn } from "cn"

import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { FieldDescription } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { IssueMessages } from "@/features/builder/components/fields/field-issues"
import { useFieldIssues } from "@/features/builder/components/fields/field-issues-context"
import { FieldsReadOnly } from "@/features/builder/components/fields/read-only"
import {
  useOpenApiSpec,
  type SpecOperation,
  type SpecSecurity,
  type SpecSource,
} from "@/features/credentials/lib/api"

/*
 * An OpenAPI tool's operations, as the spec offers them: each a tool the
 * agent may call, read by the admin API as the agent's runtime reads the
 * spec (so they're named as the agent's tools are), and ticked on or off.
 * Every one (nothing listed) takes in operations the spec adds later; a
 * list is kept as the tools' names, comma-separated.
 */

/** The names in a comma-separated list. */
const namesOf = (value: string) =>
  value
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean)

/** Whether a listed name is an operation: by its tool's name or its operationId. */
const names = (operation: SpecOperation, name: string) =>
  name === operation.name || name === operation.operation_id

// Methods in a tone each: reads in green, writes in amber, deletes in red.
const METHOD_TONE: Record<string, string> = {
  GET: "text-tone-green-foreground",
  HEAD: "text-tone-green-foreground",
  POST: "text-tone-amber-foreground",
  PUT: "text-tone-amber-foreground",
  PATCH: "text-tone-amber-foreground",
  DELETE: "text-destructive",
}

function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = React.useState(value)
  const key = JSON.stringify(value)
  React.useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), ms)
    return () => window.clearTimeout(timer)
    // The value is compared by what it holds, not which object it is.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, ms])
  return settled
}

/** How the spec says its API takes credentials, in words. */
function securityLine(security: SpecSecurity[]) {
  return security
    .map((scheme) => {
      if (scheme.type === "apiKey")
        return `an API key in the ${scheme.location} (${scheme.parameter})`
      if (scheme.type === "http")
        return scheme.scheme === "basic"
          ? "basic auth"
          : `a ${scheme.scheme || "bearer"} token`
      if (scheme.type === "oauth2") return "OAuth"
      if (scheme.type === "openIdConnect") return "OpenID Connect"
      return scheme.type
    })
    .filter(Boolean)
    .join(", or ")
}

export function OpenApiOperations({
  organizationId,
  source,
  value,
  onChange,
}: {
  organizationId: string
  /** Where the spec comes from, as the tool's settings say. */
  source: SpecSource
  /** The operations it may call, comma-separated; empty for every one. */
  value: string
  onChange: (value: string) => void
}) {
  const id = React.useId()
  const readOnly = React.useContext(FieldsReadOnly)
  const { issues, key } = useFieldIssues("operations")
  // Read once typing settles.
  const settled = useDebounced(source, 700)
  const found = useOpenApiSpec(organizationId, settled)
  const [filter, setFilter] = React.useState("")
  const picked = namesOf(value)
  const every = picked.length === 0
  const operations = found.data?.operations ?? []
  const isOn = (operation: SpecOperation) =>
    every || picked.some((name) => names(operation, name))
  // Listed, but not in the spec (any more): kept until unticked.
  const missing = found.data
    ? picked.filter((name) => !operations.some((o) => names(o, name)))
    : []

  const write = (on: SpecOperation[]) =>
    onChange(
      on.length === operations.length && !missing.length
        ? ""
        : [...on.map((o) => o.name), ...missing].join(", ")
    )
  const toggle = (operation: SpecOperation, on: boolean) =>
    write(operations.filter((o) => (o === operation ? on : isOn(o))))

  const wanted = filter.trim().toLowerCase()
  const shown = wanted
    ? operations.filter((o) =>
        [o.name, o.operation_id, o.path, o.summary, o.method]
          .join(" ")
          .toLowerCase()
          .includes(wanted)
      )
    : operations
  const nothingToRead =
    source.source === "inline" ? !source.spec.trim() : !source.url.trim()
  // What's typed hasn't settled to be read yet.
  const settling = JSON.stringify(settled) !== JSON.stringify(source)

  return (
    <div
      role="group"
      aria-labelledby={`${id}-label`}
      data-field={key}
      className="flex flex-col gap-2"
    >
      <div className="flex min-h-6 items-center justify-between gap-2">
        <span
          id={`${id}-label`}
          className="text-xs font-medium text-foreground"
        >
          Operations it may call
        </span>
        {found.data && (
          <Button
            type="button"
            variant="ghost"
            size="xs"
            className="-my-1 text-muted-foreground"
            disabled={found.isFetching}
            onClick={() => void found.refetch()}
          >
            {found.isFetching ? (
              <Spinner data-icon="inline-start" />
            ) : (
              <Icon icon="refresh" data-icon="inline-start" />
            )}
            Read again
          </Button>
        )}
      </div>

      {nothingToRead ? (
        <FieldDescription>
          Give the spec, and its operations are listed here to pick from.
        </FieldDescription>
      ) : found.isLoading || (settling && !found.data && !found.error) ? (
        <div className="flex flex-col gap-1.5" aria-busy="true">
          <Skeleton className="h-7" />
          <Skeleton className="h-7" />
          <Skeleton className="h-7" />
        </div>
      ) : found.error ? (
        <ErrorCallout
          title="Couldn't read the spec"
          action={
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void found.refetch()}
            >
              Retry
            </Button>
          }
        >
          {found.error.message}
        </ErrorCallout>
      ) : !found.data ? (
        // Not asked: its URL isn't a whole one yet.
        <FieldDescription>
          Give the spec&apos;s whole URL (https://…), and its operations are
          listed here to pick from.
        </FieldDescription>
      ) : (
        <>
          <p className="text-2xs/[1.6] text-muted-foreground">
            {[
              found.data.title &&
                `${found.data.title}${found.data.version ? ` ${found.data.version}` : ""}`,
              `${operations.length} operation${operations.length === 1 ? "" : "s"}`,
              found.data.servers[0] && `called at ${found.data.servers[0]}`,
            ]
              .filter(Boolean)
              .join(" · ")}
            {found.data.security.length > 0 &&
              `. Its API takes ${securityLine(found.data.security)}: pick a credential that signs in that way.`}
          </p>
          <label className="flex items-center gap-2 text-xs text-foreground">
            <Checkbox
              checked={every}
              disabled={readOnly}
              onCheckedChange={(on) =>
                onChange(on ? "" : operations.map((o) => o.name).join(", "))
              }
            />
            Every operation, including any the spec adds later
          </label>
          {operations.length > 8 && (
            <Input
              aria-label="Find an operation"
              placeholder="Find an operation"
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              className="h-7 text-xs"
            />
          )}
          <ul
            className="flex max-h-72 flex-col divide-y overflow-y-auto rounded-(--radius-item) border"
            tabIndex={-1}
          >
            {shown.map((operation) => {
              const rowId = `${id}-${operation.name}`
              return (
                <li
                  key={operation.name}
                  className="flex items-start gap-2.5 px-2.5 py-2"
                >
                  <Checkbox
                    id={rowId}
                    className="mt-0.5"
                    checked={isOn(operation)}
                    disabled={readOnly}
                    onCheckedChange={(on) => toggle(operation, Boolean(on))}
                  />
                  <label
                    htmlFor={rowId}
                    className="flex min-w-0 flex-1 flex-col gap-0.5"
                  >
                    <span className="flex min-w-0 items-baseline gap-2">
                      <span
                        className={cn(
                          "w-11 shrink-0 font-mono text-2xs font-medium",
                          METHOD_TONE[operation.method] ??
                            "text-muted-foreground"
                        )}
                      >
                        {operation.method}
                      </span>
                      <span className="truncate font-mono text-xs text-foreground">
                        {operation.path}
                      </span>
                    </span>
                    {operation.summary && (
                      <span className="truncate text-2xs text-muted-foreground">
                        {operation.summary}
                      </span>
                    )}
                    <span className="truncate font-mono text-2xs text-subtle">
                      {operation.name}
                    </span>
                  </label>
                </li>
              )
            })}
            {missing.map((name) => (
              <li
                key={`missing:${name}`}
                className="flex items-center gap-2.5 px-2.5 py-2"
              >
                <Checkbox
                  checked
                  disabled={readOnly}
                  onCheckedChange={() =>
                    onChange(
                      picked.filter((other) => other !== name).join(", ")
                    )
                  }
                  aria-label={`Remove ${name}`}
                />
                <span className="font-mono text-xs text-destructive">
                  {name}
                </span>
                <span className="text-2xs text-muted-foreground">
                  isn&apos;t in the spec
                </span>
              </li>
            ))}
            {shown.length === 0 && !missing.length && (
              <li className="px-2.5 py-2 text-2xs text-muted-foreground">
                {operations.length
                  ? "None matches."
                  : "The spec has no operations."}
              </li>
            )}
          </ul>
        </>
      )}
      <IssueMessages issues={issues} />
    </div>
  )
}
