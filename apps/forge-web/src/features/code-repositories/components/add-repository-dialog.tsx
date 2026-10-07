import * as React from "react"

import { ErrorCallout } from "@/components/forge/feedback"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
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
  FieldContent,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { useOpenCount } from "@/features/admin/components/dialog-state"
import { toApiError, type ApiError } from "@/lib/api/index"
import type { CodeRepositoryCreate } from "../lib/api"

/** Adds a GitHub repository to the organization, to ingest into the code graph. */
export function AddRepositoryDialog({
  open,
  onOpenChange,
  organizationName,
  description,
  onSubmit,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  organizationName: string
  /** What adding it does, when it's more than adding it to the organization. */
  description?: string
  /** Adds it; rejects with the API's error, which the dialog shows. */
  onSubmit: (input: Required<CodeRepositoryCreate>) => Promise<unknown>
}) {
  const count = useOpenCount(open)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <AddRepositoryForm
          key={count}
          organizationName={organizationName}
          description={description}
          onSubmit={onSubmit}
          onDone={() => onOpenChange(false)}
        />
      </DialogContent>
    </Dialog>
  )
}

function AddRepositoryForm({
  organizationName,
  description,
  onSubmit,
  onDone,
}: {
  organizationName: string
  description?: string
  onSubmit: (input: Required<CodeRepositoryCreate>) => Promise<unknown>
  onDone: () => void
}) {
  const id = React.useId()
  const [url, setUrl] = React.useState("")
  const [branch, setBranch] = React.useState("main")
  const [ingest, setIngest] = React.useState(true)
  const [submitted, setSubmitted] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<ApiError>()

  // The API says which field it refused: a URL the organization has, or one
  // that isn't a GitHub repository's (422, before the branch is looked at),
  // or a branch other than the one the URL's code graph follows (or the
  // organization has it on).
  const message = error?.message ?? ""
  const branchRefused =
    (error?.status === 409 &&
      (message.includes("follows branch") || message.includes("on branch"))) ||
    (error?.status === 422 && message.includes("branch name"))
  const urlRefused =
    !branchRefused && (error?.status === 409 || error?.status === 422)
  const urlError =
    submitted && !url.trim()
      ? "Paste the repository's GitHub URL."
      : urlRefused
        ? message
        : undefined
  const branchError =
    submitted && !branch.trim()
      ? "Name the branch to ingest."
      : branchRefused
        ? message
        : undefined

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitted(true)
    if (!url.trim() || !branch.trim()) return
    setPending(true)
    setError(undefined)
    try {
      await onSubmit({ url: url.trim(), branch: branch.trim(), ingest })
      onDone()
    } catch (caught) {
      setError(toApiError(caught))
    } finally {
      setPending(false)
    }
  }

  return (
    <form onSubmit={submit} noValidate className="grid gap-6">
      <DialogHeader>
        <DialogTitle>Add a code repository</DialogTitle>
        <DialogDescription>
          {description ??
            `A GitHub repository ${organizationName} ingests into the code graph, so its agents can search and read its code.`}
        </DialogDescription>
      </DialogHeader>
      {error && !urlRefused && !branchRefused && (
        <ErrorCallout title="Couldn't add the repository">
          {error.message}
        </ErrorCallout>
      )}
      <FieldGroup>
        <Field data-invalid={Boolean(urlError)}>
          <FieldLabel htmlFor={`${id}-url`}>GitHub URL</FieldLabel>
          <Input
            id={`${id}-url`}
            autoFocus
            maxLength={300}
            autoComplete="off"
            spellCheck={false}
            placeholder="https://github.com/owner/repository"
            value={url}
            aria-invalid={Boolean(urlError)}
            onChange={(event) => {
              setUrl(event.target.value)
              if (urlRefused) setError(undefined)
            }}
          />
          {urlError ? (
            <FieldError>{urlError}</FieldError>
          ) : (
            <FieldDescription>
              Public repositories need nothing more. A private one needs the
              code graph worker's GitHub token to read it.
            </FieldDescription>
          )}
        </Field>
        <Field data-invalid={Boolean(branchError)}>
          <FieldLabel htmlFor={`${id}-branch`}>Branch</FieldLabel>
          <Input
            id={`${id}-branch`}
            maxLength={255}
            autoComplete="off"
            spellCheck={false}
            value={branch}
            aria-invalid={Boolean(branchError)}
            onChange={(event) => {
              setBranch(event.target.value)
              if (branchRefused) setError(undefined)
            }}
          />
          {branchError ? (
            <FieldError>{branchError}</FieldError>
          ) : (
            <FieldDescription>
              The code graph follows one branch. When another organization has
              the repository, use its branch.
            </FieldDescription>
          )}
        </Field>
        <Field orientation="horizontal">
          <Checkbox
            id={`${id}-ingest`}
            checked={ingest}
            onCheckedChange={(on) => setIngest(on === true)}
          />
          <FieldContent>
            <FieldLabel htmlFor={`${id}-ingest`}>Ingest it now</FieldLabel>
            <FieldDescription>
              Queue its first ingestion of the branch's latest commit.
            </FieldDescription>
          </FieldContent>
        </Field>
      </FieldGroup>
      <DialogFooter>
        <DialogClose render={<Button type="button" variant="outline" />}>
          Cancel
        </DialogClose>
        <Button type="submit" disabled={pending}>
          {pending && <Spinner data-icon="inline-start" />}
          Add repository
        </Button>
      </DialogFooter>
    </form>
  )
}
