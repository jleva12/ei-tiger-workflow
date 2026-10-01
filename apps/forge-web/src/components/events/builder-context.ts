import * as React from "react"

import type { NodeIssues } from "@/lib/json-schema"

/** What the schema describes, in its copy: "event", "input"… */
export type SchemaSubject = { one: string; many: string }

export const EVENT_SUBJECT: SchemaSubject = { one: "event", many: "events" }

type BuilderState = {
  /** Problems by node, from `checkDraft`. */
  issues: Map<string, NodeIssues>
  /** Show the schema without letting anyone change it. */
  readOnly: boolean
  subject: SchemaSubject
}

/** What every row of the schema builder needs from the builder. */
export const BuilderContext = React.createContext<BuilderState>({
  issues: new Map(),
  readOnly: false,
  subject: EVENT_SUBJECT,
})
