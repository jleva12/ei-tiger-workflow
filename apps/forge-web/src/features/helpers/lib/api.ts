import { api } from "@/lib/api-instance"

/*
 * The admin API's helper agents (forge_admin/helpers/helpers.yaml), called
 * by name: one entry point for every helper a dialog or a button uses (the
 * instruction writer, the skill creator, …). The body is the helper's input.
 * A sync helper answers with what it made; a background one with its job,
 * and its jobs list says when it's done.
 */

/** A sync helper's answer. */
export type HelperAnswer<Result> = {
  helper: string
  /** What it made. */
  result: Result
  /** For the person: something to check, attach or decide. */
  notes: string[]
  /** The model it ran on (`provider/model`). */
  model: string
}

/** A background helper's job. */
export type HelperJob<Input, Result> = {
  id: string
  organization_id: string
  helper: string
  /** What it was asked. */
  input: Input
  status: "running" | "done" | "failed"
  /** What it made, once done. */
  result: Result | null
  /** For the person: what to check, connect or decide. */
  notes: string[]
  error: string | null
  model: string | null
  created_at: string
  created_by: string
  updated_at: string
}

const helperPath = (organizationId: string, name: string) =>
  `/organizations/${encodeURIComponent(organizationId)}/helpers/${encodeURIComponent(name)}`

/** Has a sync helper do its work, and waits for what it made. */
export function callHelper<Input, Result>(
  organizationId: string,
  name: string,
  input: Input,
  { signal }: { signal?: AbortSignal } = {}
) {
  return api.post<HelperAnswer<Result>, Input>(
    helperPath(organizationId, name),
    input,
    { signal, silent: true }
  )
}

/** Starts a background helper: its job, running. */
export function startHelper<Input, Result>(
  organizationId: string,
  name: string,
  input: Input
) {
  return api.post<HelperJob<Input, Result>, Input>(
    helperPath(organizationId, name),
    input,
    { silent: true }
  )
}

/** A background helper's jobs from the last week, newest first. */
export function helperJobs<Input, Result>(
  organizationId: string,
  name: string,
  { signal }: { signal?: AbortSignal } = {}
) {
  return api.get<HelperJob<Input, Result>[]>(
    `${helperPath(organizationId, name)}/jobs`,
    { signal }
  )
}

/** Takes a job off the list; one still running goes on. */
export function dismissHelperJob(
  organizationId: string,
  name: string,
  id: string
) {
  return api.delete<void>(
    `${helperPath(organizationId, name)}/jobs/${encodeURIComponent(id)}`
  )
}
