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
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import {
  useLastNode,
  useOpenCount,
} from "@/features/admin/components/dialog-state"
import { toApiError, type ApiError } from "@/lib/api/index"
import type { KnowledgeBase, KnowledgeBaseKind } from "../lib/api"

/** What the dialog saves: a new knowledge base's kind is the caller's. */
export type KnowledgeBaseInput = { name: string; description: string }

/** Creates a knowledge base of a kind, or renames and describes one. */
export function KnowledgeBaseDialog({
  open,
  onOpenChange,
  base,
  kind = "rag",
  organizationName,
  onSubmit,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The knowledge base to edit; without one the dialog creates one. */
  base?: KnowledgeBase
  /** What a new one holds: documents (`rag`) or code repositories (`graph`). */
  kind?: KnowledgeBaseKind
  organizationName: string
  /** Saves; rejects with the API's error, which the dialog shows. */
  onSubmit: (input: KnowledgeBaseInput) => Promise<unknown>
}) {
  const count = useOpenCount(open)
  // While it closes, it keeps showing what it showed.
  const shown = useLastNode(base)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <KnowledgeBaseForm
          key={count}
          base={open ? base : shown}
          kind={kind}
          organizationName={organizationName}
          onSubmit={onSubmit}
          onDone={() => onOpenChange(false)}
        />
      </DialogContent>
    </Dialog>
  )
}

const COPY: Record<
  KnowledgeBaseKind,
  {
    title: string
    about: (organization: string) => string
    placeholder: string
  }
> = {
  rag: {
    title: "New RAG knowledge base",
    about: (organization) =>
      `A set of ${organization}'s documents its agents can search: runbooks, specs, policies. Upload them once it's made.`,
    placeholder: "Engineering handbook",
  },
  graph: {
    title: "New graph knowledge base",
    about: (organization) =>
      `A set of ${organization}'s code repositories its agents can search, ingested into the code graph: each declaration, how it connects, and its source. Add the repositories once it's made.`,
    placeholder: "Payments services",
  },
}

function KnowledgeBaseForm({
  base,
  kind,
  organizationName,
  onSubmit,
  onDone,
}: {
  base?: KnowledgeBase
  kind: KnowledgeBaseKind
  organizationName: string
  onSubmit: (input: KnowledgeBaseInput) => Promise<unknown>
  onDone: () => void
}) {
  const copy = COPY[base?.kind ?? kind]
  const id = React.useId()
  const [name, setName] = React.useState(base?.name ?? "")
  const [description, setDescription] = React.useState(base?.description ?? "")
  const [submitted, setSubmitted] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<ApiError>()

  // A name another knowledge base has comes back as 409; say so on the field.
  const nameError =
    submitted && !name.trim()
      ? "Give the knowledge base a name."
      : error?.status === 409
        ? error.message
        : undefined

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitted(true)
    if (!name.trim()) return
    setPending(true)
    setError(undefined)
    try {
      await onSubmit({ name: name.trim(), description: description.trim() })
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
        <DialogTitle>{base ? "Edit knowledge base" : copy.title}</DialogTitle>
        <DialogDescription>
          {base
            ? `Rename or describe ${base.name}.`
            : copy.about(organizationName)}
        </DialogDescription>
      </DialogHeader>
      {error && error.status !== 409 && (
        <ErrorCallout
          title={`Couldn't ${base ? "save" : "create"} the knowledge base`}
        >
          {error.message}
        </ErrorCallout>
      )}
      <FieldGroup>
        <Field data-invalid={Boolean(nameError)}>
          <FieldLabel htmlFor={`${id}-name`}>Name</FieldLabel>
          <Input
            id={`${id}-name`}
            autoFocus
            maxLength={200}
            autoComplete="off"
            placeholder={copy.placeholder}
            value={name}
            aria-invalid={Boolean(nameError)}
            onChange={(event) => {
              setName(event.target.value)
              if (error?.status === 409) setError(undefined)
            }}
          />
          {nameError && <FieldError>{nameError}</FieldError>}
        </Field>
        <Field>
          <FieldLabel htmlFor={`${id}-description`}>Description</FieldLabel>
          <Textarea
            id={`${id}-description`}
            rows={3}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
          <FieldDescription>
            Optional. Say what's in it, so people pick the right one for an
            agent.
          </FieldDescription>
        </Field>
      </FieldGroup>
      <DialogFooter>
        <DialogClose render={<Button type="button" variant="outline" />}>
          Cancel
        </DialogClose>
        <Button type="submit" disabled={pending}>
          {pending && <Spinner data-icon="inline-start" />}
          {base ? "Save" : "Create knowledge base"}
        </Button>
      </DialogFooter>
    </form>
  )
}
