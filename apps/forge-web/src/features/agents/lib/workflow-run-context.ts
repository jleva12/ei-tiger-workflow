import * as React from "react"

import type { WorkflowRunFollower } from "./workflow-runs"

/**
 * How the chat's workflow run cards read their runs: the runtime its chat
 * talks to, and the workflow tools' names. None draws a card from the
 * call's answer alone.
 */
export const WorkflowRunsContext =
  React.createContext<WorkflowRunFollower | null>(null)
