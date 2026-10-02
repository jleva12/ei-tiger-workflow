import * as React from "react"
import { PlayIcon } from "@hugeicons/core-free-icons"
import type { UseMutationResult } from "@tanstack/react-query"

import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Spinner } from "@/components/ui/spinner"
import type { ApiError } from "@/lib/api/index"
import { useSchemaValue } from "@/features/runs/lib/schema-value"
import { SchemaValueEditor } from "./schema-value-editor"

/*
 * Running a workflow (or an ADK workflow) by hand: its input, as the fields
 * its start declares (nested objects as groups; lists and free-form objects
 * as JSON), or as JSON outright. The API checks the input against the start
 * and answers why it doesn't fit, which shows here.
 */

/** Starting a run with an input: what it answers once it's queued. */
export type RunMutation<T> = UseMutationResult<T, ApiError, unknown>

/** What the dialog says about what it runs. */
export type RunDialogWords = {
  /** What running it does, under the title. */
  description: React.ReactNode
  /** A 422's title: the API refused the input (or what it runs). */
  refused: string
  /** A 403's title, and whose permission running takes. */
  forbidden: { title: string; description: string }
}

/**
 * The input, and Run. Mounted each time the dialog opens, so every run
 * starts from empty fields.
 */
function RunForm<T>({
  run,
  words,
  inputSchema,
  onDone,
}: {
  run: RunMutation<T>
  words: RunDialogWords
  inputSchema: Record<string, unknown>
  onDone: (run: T) => void
}) {
  const input = useSchemaValue(inputSchema)

  const submit = (event: React.SubmitEvent<HTMLFormElement>) => {
    event.preventDefault()
    const read = input.read()
    if (!read.ok) return
    run.mutate(read.value, { onSuccess: onDone })
  }

  const busy = run.isPending
  return (
    <form onSubmit={submit} className="flex min-h-0 flex-1 flex-col" noValidate>
      <DialogHeader className="gap-1.5 border-b border-border px-5 pt-5 pb-4">
        <DialogTitle>Start a run</DialogTitle>
        <DialogDescription>{words.description}</DialogDescription>
      </DialogHeader>
      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-5 py-4" tabIndex={-1}>
        <SchemaValueEditor
          value={input}
          title="Input"
          toggleLabel="Edit the input as"
          disabled={busy}
          jsonPlaceholder="Optional: any JSON the steps read as input"
          noFields="The start step declares no fields, so steps read this as it is."
        />
        {run.error && (
          <ErrorCallout
            title={
              run.error.status === 422
                ? words.refused
                : run.error.status === 403
                  ? words.forbidden.title
                  : "Couldn't start the run"
            }
          >
            {run.error.status === 403 ? words.forbidden.description : run.error.message}
          </ErrorCallout>
        )}
      </div>
      <DialogFooter className="border-t border-border px-5 py-3.5">
        <DialogClose render={<Button variant="outline" type="button" disabled={busy} />}>
          Cancel
        </DialogClose>
        <Button type="submit" disabled={busy}>
          {busy ? (
            <Spinner data-icon="inline-start" />
          ) : (
            <Icon icon={PlayIcon} data-icon="inline-start" />
          )}
          Run
        </Button>
      </DialogFooter>
    </form>
  )
}

/**
 * Start a run: its input, and Run. `run` starts it (the caller's, so a run
 * being submitted keeps the dialog open); what it runs speaks through
 * `words`.
 */
export function RunDialog<T>({
  open,
  onOpenChange,
  run,
  words,
  inputSchema,
  onStarted,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  run: RunMutation<T>
  words: RunDialogWords
  /** The start's input schema; `{}` declares none. */
  inputSchema: Record<string, unknown>
  onStarted: (run: T) => void
}) {
  const close = () => {
    run.reset()
    onOpenChange(false)
  }
  return (
    <Dialog open={open} onOpenChange={(next) => !next && !run.isPending && close()}>
      <DialogContent className="flex max-h-[min(44rem,calc(100dvh-4rem))] flex-col gap-0 p-0 sm:max-w-lg">
        <RunForm
          run={run}
          words={words}
          inputSchema={inputSchema}
          onDone={(started) => {
            close()
            onStarted(started)
          }}
        />
      </DialogContent>
    </Dialog>
  )
}
