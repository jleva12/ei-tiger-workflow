import * as React from "react"

import type { NodeIssues } from "@/features/json/lib/json-schema"

/** What the schema describes, in its copy: "input", "answer"… */
export type SchemaSubject = { one: string; many: string }

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
  subject: { one: "value", many: "values" },
})
