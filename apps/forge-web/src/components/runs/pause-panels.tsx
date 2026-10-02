import * as React from "react"
import type { UseMutationResult } from "@tanstack/react-query"

import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import type { IconProp } from "@/components/forge/icons"
import { Chip } from "@/components/forge/status"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import type { ApiError } from "@/lib/api"
import {
  APPROVERS,
  type AdkRunPause,
  type ApprovalDecision,
} from "@/lib/agents/runs"
import { formatRelative } from "@/lib/format"
import { useScopeAccess, type PermissionKey } from "@/lib/hierarchy"
import { useSchemaValue } from "./schema-value"
import { SchemaValueEditor } from "./schema-value-editor"

/** Deciding the approval an ADK workflow run waits at. */
export type DecideMutation = UseMutationResult<
  unknown,
  ApiError,
  ApprovalDecision & { requestId: string }
>

/** Answering what a run asks a person. */
export type AnswerMutation = UseMutationResult<
  unknown,
  ApiError,
  { requestId: string; answer: unknown }
>

/**
 * The warm frame of what a run waits for: its question, where it was asked
 * (`meta`), who answers it (`chip`), then what they can do.
 */
function PauseFrame({
  icon,
  title,
  meta,
  chip,
  children,
}: {
  icon: IconProp
  title: React.ReactNode
  meta: string
  chip?: React.ReactNode
  children: React.ReactNode
}) {
  const id = React.useId()
  return (
    <section
      aria-labelledby={`${id}-title`}
      className="mb-[34px] flex gap-3.5 rounded-(--radius-card) border border-notice-border bg-notice-surface p-5 @max-[600px]/shell:p-[15px]"
    >
      <span className="grid size-10 shrink-0 place-items-center rounded-full border border-notice-border bg-notice-icon-surface text-notice-accent @max-[600px]/shell:hidden">
        <Icon icon={icon} />
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-4">
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
          <div className="flex min-w-0 flex-col gap-1">
            <h2 id={`${id}-title`} className="text-lg font-[550] text-pretty">
              {title}
            </h2>
            <p className="text-sm/[1.6] text-notice-foreground">{meta}</p>
          </div>
          {chip}
        </div>
        {children}
      </div>
    </section>
  )
}

/** Where a question was asked: its step, its workflow, when. */
function askedLine(pause: AdkRunPause, deadline = false) {
  const step = pause.details.step_name || pause.details.step
  const workflow = pause.details.workflow_name
  return [
    step && `${step} step`,
    workflow,
    pause.requested_at && `asked ${formatRelative(pause.requested_at)}`,
    deadline &&
      pause.deadline &&
      `expires ${formatRelative(pause.deadline)}`,
  ]
    .filter(Boolean)
    .join(" · ")
}

/**
 * What a run waits for: its approval step's question, who may decide it,
 * and, for them (`permission`), a comment and Approve or Reject through
 * `decide`. The run carries on down the step's approved or rejected way;
 * the comment and who decided are what its later steps read.
 */
export function ApprovalPanel({
  organizationId,
  pause,
  decide,
  permission,
  docs = "ADK workflows",
  showDeadline = false,
}: {
  organizationId: string
  pause: AdkRunPause
  decide: DecideMutation
  /** What deciding it takes in the organization. */
  permission: PermissionKey
  /** What its members run, in "Any member who can run the organization's …". */
  docs?: string
  /** Say when its time is up, for approvals that expire. */
  showDeadline?: boolean
}) {
  const id = React.useId()
  const can = useScopeAccess(`org:${organizationId}`)
  const approvers = pause.details.approvers ?? "org:admin"
  const allowed = can(permission)
  const [comment, setComment] = React.useState("")
  const [choice, setChoice] = React.useState<boolean>()

  const submit = (approved: boolean) => {
    setChoice(approved)
    decide.mutate({ requestId: pause.id, approved, comment: comment.trim() })
  }

  return (
    <PauseFrame
      icon="review"
      title={pause.reason}
      meta={askedLine(pause, showDeadline)}
      chip={
        <Chip tone="notice">
          {APPROVERS[approvers] ?? APPROVERS["org:admin"]}
        </Chip>
      }
    >
      {allowed ? (
        <>
          <Field>
            <FieldLabel htmlFor={`${id}-comment`}>Comment</FieldLabel>
            <Textarea
              id={`${id}-comment`}
              rows={2}
              value={comment}
              maxLength={4000}
              disabled={decide.isPending || decide.isSuccess}
              placeholder="Optional: why, for the record"
              onChange={(event) => setComment(event.target.value)}
              className="bg-background"
            />
            <FieldDescription>
              The run&apos;s later steps can read it, with who decided.
            </FieldDescription>
          </Field>
          {decide.error && (
            <ErrorCallout title="Couldn't record the decision">
              {decide.error.status === 409
                ? "Someone decided it first, or the run moved on. The page shows where it is now."
                : decide.error.message}
            </ErrorCallout>
          )}
          <div className="flex flex-wrap gap-2">
            <Button
              disabled={decide.isPending || decide.isSuccess}
              onClick={() => submit(true)}
            >
              {decide.isPending && choice === true ? (
                <Spinner data-icon="inline-start" />
              ) : (
                <Icon icon="check" data-icon="inline-start" />
              )}
              Approve
            </Button>
            <Button
              variant="outline"
              disabled={decide.isPending || decide.isSuccess}
              onClick={() => submit(false)}
            >
              {decide.isPending && choice === false ? (
                <Spinner data-icon="inline-start" />
              ) : (
                <Icon icon="close" data-icon="inline-start" />
              )}
              Reject
            </Button>
          </div>
        </>
      ) : (
        <p className="text-sm/[1.6] text-notice-foreground">
          {approvers === "org:member"
            ? `Any member who can run the organization's ${docs} decides it.`
            : "One of the organization's admins decides it."}{" "}
          The run waits until then.
        </p>
      )}
    </PauseFrame>
  )
}

/**
 * What a run asks a person (an ADK workflow's human input): its question,
 * and, for those who may answer (`permission`), a form of the fields its
 * `responseSchema` declares (or JSON, or plain text when it declares none),
 * sent through `answer`. The API holds the answer to the schema and says
 * why one doesn't fit, which shows here. The run carries on with it.
 */
export function HumanInputPanel({
  organizationId,
  pause,
  responseSchema,
  answer,
  permission,
  docs = "ADK workflows",
}: {
  organizationId: string
  pause: AdkRunPause
  /** A JSON Schema of the answer; `{}` takes anything. */
  responseSchema: Record<string, unknown>
  answer: AnswerMutation
  /** What answering it takes in the organization. */
  permission: PermissionKey
  /** What its members run, in "Any member who can run the organization's …". */
  docs?: string
}) {
  const can = useScopeAccess(`org:${organizationId}`)
  const allowed = can(permission)
  const value = useSchemaValue(responseSchema, { lenient: true })
  const busy = answer.isPending || answer.isSuccess
  const message =
    pause.reason ||
    (typeof pause.details.message === "string" &&
      pause.details.message) ||
    "It asks for an answer"

  const submit = (event: React.SubmitEvent<HTMLFormElement>) => {
    event.preventDefault()
    const read = value.read()
    if (!read.ok) return
    answer.mutate({ requestId: pause.id, answer: read.value })
  }

  return (
    <PauseFrame
      icon="message"
      title={message}
      meta={askedLine(pause)}
      chip={<Chip tone="notice">Waiting for an answer</Chip>}
    >
      {allowed ? (
        <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
          <SchemaValueEditor
            value={value}
            title="Your answer"
            toggleLabel="Edit your answer as"
            disabled={busy}
            jsonPlaceholder="Your answer: text, or any JSON"
            noFields="It asks for no fields: plain text is sent as it is, JSON as its value."
          />
          {answer.error && (
            <ErrorCallout
              title={
                answer.error.status === 422
                  ? "Check your answer"
                  : "Couldn't send the answer"
              }
            >
              {answer.error.status === 409
                ? "Someone answered it first, or the run moved on. The page shows where it is now."
                : answer.error.status === 403
                  ? `Answering it takes ${permission} in the organization. Ask an organization admin.`
                  : answer.error.message}
            </ErrorCallout>
          )}
          <div className="flex flex-wrap gap-2">
            <Button type="submit" disabled={busy}>
              {answer.isPending ? (
                <Spinner data-icon="inline-start" />
              ) : (
                <Icon icon="check" data-icon="inline-start" />
              )}
              Send answer
            </Button>
          </div>
        </form>
      ) : (
        <p className="text-sm/[1.6] text-notice-foreground">
          Any member who can run the organization&apos;s {docs} answers it. The
          run waits until then.
        </p>
      )}
    </PauseFrame>
  )
}
