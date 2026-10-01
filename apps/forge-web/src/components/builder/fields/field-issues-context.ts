import * as React from "react"

import type { BuilderIssue } from "@/lib/builder/types"

/*
 * The issues of the step whose settings are open, for its fields: a field
 * names the setting it holds (a config key, a row's `cases.<id>`), shows
 * its issues under it, and turns red for an error. A sub-agent's settings
 * name theirs inside `agents.<id>.` (FieldIssuesPrefix, ./field-issues).
 */

export type FieldIssues = { issues: BuilderIssue[]; prefix: string }

export const FieldIssuesContext = React.createContext<FieldIssues>({
  issues: [],
  prefix: "",
})

const errorsFirst = (a: BuilderIssue, b: BuilderIssue) =>
  a.level === b.level ? 0 : a.level === "error" ? -1 : 1

/**
 * The issues of settings, by their key under the prefix: `of(key)` a
 * setting's own, `under(key)` its and those of what's inside it (a list's
 * rows, a sub-agent's settings); `key(key)` its full key.
 */
export function useIssueLookup({ whole = false } = {}) {
  const context = React.useContext(FieldIssuesContext)
  const { issues } = context
  const prefix = whole ? "" : context.prefix
  return React.useMemo(() => {
    const key = (field: string) => prefix + field
    return {
      key,
      of: (field: string | undefined) =>
        field === undefined
          ? []
          : issues.filter((i) => i.field === key(field)).sort(errorsFirst),
      under: (field: string) =>
        issues
          .filter(
            (i) =>
              i.field === key(field) || i.field?.startsWith(`${key(field)}.`)
          )
          .sort(errorsFirst),
    }
  }, [issues, prefix])
}

/**
 * A setting's issues, and whether one is an error; its key for finding it.
 * `whole`: `field` is the setting's whole key, not one under the prefix.
 */
export function useFieldIssues(
  field: string | undefined,
  { whole = false } = {}
) {
  const lookup = useIssueLookup({ whole })
  const issues = lookup.of(field)
  return {
    issues,
    invalid: issues.some((i) => i.level === "error"),
    key: field === undefined ? undefined : lookup.key(field),
  }
}
