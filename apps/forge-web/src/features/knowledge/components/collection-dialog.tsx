import * as React from "react"

import {
  useLastNode,
  useOpenCount,
} from "@/features/admin/components/dialog-state"
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
import { toApiError, type ApiError } from "@/lib/api/index"
import type { CollectionInput, DocumentCollection } from "../lib/api"

/** Creates a document collection, or renames and describes one. */
export function CollectionDialog({
  open,
  onOpenChange,
  collection,
  parent,
  baseName,
  onSubmit,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The collection to edit; without one the dialog creates one. */
  collection?: DocumentCollection
  /** Where a new one goes; the top of the knowledge base when null. */
  parent: DocumentCollection | null
  baseName: string
  /** Saves; rejects with the API's error, which the dialog shows. */
  onSubmit: (input: CollectionInput) => Promise<unknown>
}) {
  const count = useOpenCount(open)
  const shown = useLastNode(collection)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <CollectionForm
          key={count}
          collection={open ? collection : shown}
          parent={parent}
          baseName={baseName}
          onSubmit={onSubmit}
          onDone={() => onOpenChange(false)}
        />
      </DialogContent>
    </Dialog>
  )
}

function CollectionForm({
  collection,
  parent,
  baseName,
  onSubmit,
  onDone,
}: {
  collection?: DocumentCollection
  parent: DocumentCollection | null
  baseName: string
  onSubmit: (input: CollectionInput) => Promise<unknown>
  onDone: () => void
}) {
  const id = React.useId()
  const [name, setName] = React.useState(collection?.name ?? "")
  const [description, setDescription] = React.useState(
    collection?.description ?? ""
  )
  const [submitted, setSubmitted] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<ApiError>()

  // A name a sibling already has comes back as 409; say so on the field.
  const nameError =
    submitted && !name.trim()
      ? "Give the collection a name."
      : error?.status === 409
        ? error.message
        : undefined

  async function submit(event: React.SubmitEvent<HTMLFormElement>) {
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
        <DialogTitle>
          {collection
            ? "Edit collection"
            : parent
              ? `New collection in ${parent.name}`
              : "New collection"}
        </DialogTitle>
        <DialogDescription>
          {collection
            ? `Rename or describe ${collection.name}.`
            : `Group ${baseName}'s documents so people find them faster; collections can hold collections, like folders. Agents search every document, whatever its collection.`}
        </DialogDescription>
      </DialogHeader>
      {error && error.status !== 409 && (
        <ErrorCallout
          title={`Couldn't ${collection ? "save" : "create"} the collection`}
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
            placeholder="Runbooks"
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
            Optional. Shown on the collection.
          </FieldDescription>
        </Field>
      </FieldGroup>
      <DialogFooter>
        <DialogClose render={<Button type="button" variant="outline" />}>
          Cancel
        </DialogClose>
        <Button type="submit" disabled={pending}>
          {pending && <Spinner data-icon="inline-start" />}
          {collection ? "Save" : "Create collection"}
        </Button>
      </DialogFooter>
    </form>
  )
}
