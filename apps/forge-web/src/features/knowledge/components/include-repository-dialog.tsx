import * as React from "react"

import { ErrorCallout } from "@/components/forge/feedback"
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
import {
  Field,
  FieldDescription,
  FieldError,
  FieldLabel,
} from "@/components/ui/field"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { useOpenCount } from "@/features/admin/components/dialog-state"
import type { CodeRepository } from "@/features/code-repositories/lib/api"
import {
  fullName,
  statusDisplay,
} from "@/features/code-repositories/lib/display"
import { toApiError, type ApiError } from "@/lib/api/index"

/**
 * Includes one of the organization's code repositories in a system
 * design knowledge base: those it doesn't include yet, from the organization's
 * Code repositories tab (Knowledge bases → Code repositories).
 */
export function IncludeRepositoryDialog({
  open,
  onOpenChange,
  baseName,
  organizationName,
  repositories,
  onAddNew,
  onSubmit,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  baseName: string
  organizationName: string
  /** The organization's repositories the knowledge base doesn't include; undefined while they load. */
  repositories: CodeRepository[] | undefined
  /** Switches to adding a GitHub repository the organization doesn't have. */
  onAddNew: () => void
  /** Includes it; rejects with the API's error, which the dialog shows. */
  onSubmit: (repositoryId: string) => Promise<unknown>
}) {
  const count = useOpenCount(open)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <IncludeRepositoryForm
          key={count}
          baseName={baseName}
          organizationName={organizationName}
          repositories={repositories}
          onAddNew={onAddNew}
          onSubmit={onSubmit}
          onDone={() => onOpenChange(false)}
        />
      </DialogContent>
    </Dialog>
  )
}

function IncludeRepositoryForm({
  baseName,
  organizationName,
  repositories,
  onAddNew,
  onSubmit,
  onDone,
}: {
  baseName: string
  organizationName: string
  repositories: CodeRepository[] | undefined
  onAddNew: () => void
  onSubmit: (repositoryId: string) => Promise<unknown>
  onDone: () => void
}) {
  const id = React.useId()
  const [picked, setPicked] = React.useState<string>()
  const [submitted, setSubmitted] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<ApiError>()
  const items = React.useMemo(
    () =>
      (repositories ?? []).map((repository) => ({
        value: repository.id,
        label: fullName(repository),
      })),
    [repositories]
  )
  const missing = submitted && !picked

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitted(true)
    if (!picked) return
    setPending(true)
    setError(undefined)
    try {
      await onSubmit(picked)
      onDone()
    } catch (caught) {
      setError(toApiError(caught))
    } finally {
      setPending(false)
    }
  }

  const none = repositories !== undefined && repositories.length === 0
  return (
    <form onSubmit={submit} noValidate className="grid gap-6">
      <DialogHeader>
        <DialogTitle>Add one of {organizationName}'s repositories</DialogTitle>
        <DialogDescription>
          {`Agents searching ${baseName} search its code too, once an ingestion of it has succeeded. It stays in ${organizationName}, and in any other knowledge base that has it.`}
        </DialogDescription>
      </DialogHeader>
      {error && (
        <ErrorCallout title="Couldn't add the repository">
          {error.message}
        </ErrorCallout>
      )}
      {none ? (
        <p className="text-sm text-muted-foreground">
          {`${baseName} has every one of ${organizationName}'s repositories already. Add a GitHub repository instead.`}
        </p>
      ) : (
        <Field data-invalid={missing}>
          <FieldLabel htmlFor={`${id}-repository`}>Repository</FieldLabel>
          <Select
            items={items}
            value={picked ?? null}
            onValueChange={(value) => value && setPicked(String(value))}
          >
            <SelectTrigger
              id={`${id}-repository`}
              className="w-full"
              aria-invalid={missing}
              disabled={repositories === undefined}
            >
              <SelectValue placeholder="Pick a repository" />
            </SelectTrigger>
            <SelectContent alignItemWithTrigger={false}>
              {(repositories ?? []).map((repository) => {
                const latest = repository.latest_ingestion
                return (
                  <SelectItem key={repository.id} value={repository.id}>
                    <span className="flex min-w-0 flex-1 items-center justify-between gap-3">
                      <span className="truncate">{fullName(repository)}</span>
                      <span className="shrink-0 text-2xs text-muted-foreground">
                        {latest ? statusDisplay(latest).label : "Not ingested"}
                      </span>
                    </span>
                  </SelectItem>
                )
              })}
            </SelectContent>
          </Select>
          {missing ? (
            <FieldError>Pick the repository to add.</FieldError>
          ) : (
            <FieldDescription>
              One not ingested yet is searched once you ingest it.
            </FieldDescription>
          )}
        </Field>
      )}
      <DialogFooter className="sm:justify-between">
        <Button
          type="button"
          variant="link"
          className="px-0"
          onClick={onAddNew}
        >
          Add a GitHub repository instead
        </Button>
        <span className="flex gap-2">
          <DialogClose render={<Button type="button" variant="outline" />}>
            Cancel
          </DialogClose>
          <Button type="submit" disabled={pending || none}>
            {pending && <Spinner data-icon="inline-start" />}
            Add repository
          </Button>
        </span>
      </DialogFooter>
    </form>
  )
}
