/*
 * Who may use an agent on the canvas: the access rule on its node
 * (`config.access`), which the runtime checks for whoever the call is for
 * as the agent runs (forge_agent_runtime.access). A rule is one of the
 * organization's agent permissions, by ID, or a group of rules: `all`
 * (every one holds) or `any` (at least one does); null lets in anyone who
 * may call the agent. So a coordinator may let someone in while one of
 * its sub-agents doesn't: that one answers with what it needs.
 */

export type AccessRule =
  { permission: number } | { all: AccessRule[] } | { any: AccessRule[] }

/** How deep groups may nest: a group of groups of groups (the runtime's MAX_DEPTH). */
export const MAX_RULE_DEPTH = 3
/** The most permissions one rule may name (the runtime's MAX_PERMISSIONS). */
export const MAX_RULE_PERMISSIONS = 50

/** How a group's rules combine. */
export type RuleMode = "all" | "any"

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)

/**
 * A rule from whatever a document holds: null for none, undefined when it
 * isn't one (wrong shape, nested too deep, too many permissions).
 */
export function readAccessRule(value: unknown): AccessRule | null | undefined {
  if (value === null || value === undefined) return null
  let count = 0
  const read = (raw: unknown, depth: number): AccessRule | undefined => {
    if (!isRecord(raw) || Object.keys(raw).length !== 1) return undefined
    if ("permission" in raw) {
      const id = raw.permission
      if (typeof id !== "number" || !Number.isInteger(id) || id < 1)
        return undefined
      count += 1
      return count > MAX_RULE_PERMISSIONS ? undefined : { permission: id }
    }
    const mode = "all" in raw ? "all" : "any" in raw ? "any" : null
    if (!mode || depth >= MAX_RULE_DEPTH) return undefined
    const items = raw[mode]
    if (!Array.isArray(items) || items.length === 0) return undefined
    const rules = items.map((item) => read(item, depth + 1))
    if (rules.some((item) => item === undefined)) return undefined
    const checked = rules as AccessRule[]
    return mode === "all" ? { all: checked } : { any: checked }
  }
  return read(value, 0)
}

/** The IDs of the permissions a rule names, each once, in the order it names them. */
export function rulePermissions(rule: AccessRule | null): number[] {
  if (!rule) return []
  if ("permission" in rule) return [rule.permission]
  const items = "all" in rule ? rule.all : rule.any
  return [...new Set(items.flatMap(rulePermissions))]
}

const joined = (parts: string[], conjunction: string) =>
  parts.length === 2
    ? `${parts[0]} ${conjunction} ${parts[1]}`
    : `${parts.slice(0, -1).join(", ")}, ${conjunction} ${parts[parts.length - 1]}`

/**
 * A rule as people say it, as the runtime's refusals do: "invoices:approve,
 * or both finance:read and finance:write".
 *
 * @param name A permission's name by its ID; undefined for one that was deleted.
 */
export function ruleWords(
  rule: AccessRule,
  name: (id: number) => string | undefined,
  nested = false
): string {
  if ("permission" in rule)
    return name(rule.permission) ?? `a deleted permission (#${rule.permission})`
  const every = "all" in rule
  const parts = (every ? rule.all : rule.any).map((item) =>
    ruleWords(item, name, true)
  )
  if (parts.length === 1) return parts[0]
  const said = joined(parts, every ? "and" : "or")
  if (!nested) return said
  if (every) return parts.length === 2 ? `both ${said}` : `all of ${said}`
  return parts.length === 2 ? `either ${said}` : `one of ${said}`
}

/*
 * The rule editor's draft: a root group whose items are permissions (one
 * not picked yet is null) and groups, each with an id of its own for
 * React's keys while they move.
 */

export type DraftPermission = {
  id: string
  type: "permission"
  permission: number | null
}
export type DraftGroup = {
  id: string
  type: "group"
  mode: RuleMode
  items: DraftItem[]
}
export type DraftItem = DraftPermission | DraftGroup

let made = 0
/** A new id for a draft item. */
export const draftId = () => `rule_${(made += 1)}`

/** A new, empty group. */
export const emptyGroup = (mode: RuleMode = "any"): DraftGroup => ({
  id: draftId(),
  type: "group",
  mode,
  items: [],
})

/** The editor's draft of a rule: a group at the root, empty for none. */
export function toDraft(rule: AccessRule | null): DraftGroup {
  const item = (r: AccessRule): DraftItem =>
    "permission" in r
      ? { id: draftId(), type: "permission", permission: r.permission }
      : {
          id: draftId(),
          type: "group",
          mode: "all" in r ? "all" : "any",
          items: ("all" in r ? r.all : r.any).map(item),
        }
  if (!rule) return emptyGroup()
  const root = item(rule)
  return root.type === "group" ? root : { ...emptyGroup(), items: [root] }
}

/**
 * The rule a draft says: permissions not picked yet and empty groups left
 * out; null when nothing is left.
 */
export function fromDraft(group: DraftGroup): AccessRule | null {
  const rule = (item: DraftItem): AccessRule | null => {
    if (item.type === "permission")
      return item.permission === null ? null : { permission: item.permission }
    const items = item.items
      .map(rule)
      .filter((r): r is AccessRule => r !== null)
    if (items.length === 0) return null
    return item.mode === "all" ? { all: items } : { any: items }
  }
  return rule(group)
}

/** How deep a draft's groups nest: 1 for the root alone. */
export function draftDepth(group: DraftGroup): number {
  const deepest = group.items.reduce(
    (most, item) =>
      item.type === "group" ? Math.max(most, draftDepth(item)) : most,
    0
  )
  return deepest + 1
}

/** Every permission a draft names, picked or not. */
export function draftPermissionCount(group: DraftGroup): number {
  return group.items.reduce(
    (count, item) =>
      count + (item.type === "group" ? draftPermissionCount(item) : 1),
    0
  )
}
