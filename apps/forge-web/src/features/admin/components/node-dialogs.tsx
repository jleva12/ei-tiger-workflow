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
import { toApiError, type ApiError } from "@/lib/api/index"
import type { NodeInput, Organization } from "@/lib/hierarchy"
import type { Level } from "./levels"
import { useLastNode, useOpenCount } from "./dialog-state"

type NodeDialogProps = {
  level: Level
  open: boolean
  onOpenChange: (open: boolean) => void
  /** The node to edit; without one the dialog creates a node. */
  node?: Organization
  /** Where a new node goes, e.g. the organization's name. */
  parentName?: string
  /**
   * Saves, given the node being edited (if any); rejects with the API's
   * error, which the dialog shows.
   */
  onSubmit: (input: NodeInput, node?: Organization) => Promise<unknown>
}

/** Creates or edits an organization. */
export function NodeDialog({
  open,
  onOpenChange,
  node,
  ...props
}: NodeDialogProps) {
  const count = useOpenCount(open)
  const shown = useLastNode(node)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <NodeForm
          key={count}
          node={shown}
          onDone={() => onOpenChange(false)}
          {...props}
        />
      </DialogContent>
    </Dialog>
  )
}

function NodeForm({
  level,
  node,
  parentName,
  onSubmit,
  onDone,
}: Omit<NodeDialogProps, "open" | "onOpenChange"> & { onDone: () => void }) {
  const id = React.useId()
  const [name, setName] = React.useState(node?.name ?? "")
  const [description, setDescription] = React.useState(node?.description ?? "")
  const [submitted, setSubmitted] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<ApiError>()

  // A duplicate name comes back as 409 with the reason; show it on the field.
  const nameError =
    submitted && !name.trim()
      ? `Give the ${level.noun} a name.`
      : error?.status === 409
        ? error.message
        : undefined
  const verb = node ? "save" : "create"

  async function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitted(true)
    if (!name.trim()) return
    setPending(true)
    setError(undefined)
    try {
      const input: NodeInput = {
        name: name.trim(),
        description: description.trim(),
      }
      await onSubmit(input, node)
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
          {node ? `Edit ${level.noun}` : `New ${level.noun}`}
        </DialogTitle>
        <DialogDescription>
          {node
            ? `Rename or describe ${node.name}.`
            : parentName
              ? `Add ${level.article} ${level.noun} to ${parentName}.`
              : `Add ${level.article} ${level.noun}.`}
        </DialogDescription>
      </DialogHeader>
      {error && error.status !== 409 && (
        <ErrorCallout title={`Couldn't ${verb} the ${level.noun}`}>
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
          <FieldDescription>Optional.</FieldDescription>
        </Field>
      </FieldGroup>
      <DialogFooter>
        <DialogClose render={<Button type="button" variant="outline" />}>
          Cancel
        </DialogClose>
        <Button type="submit" disabled={pending}>
          {pending && <Spinner data-icon="inline-start" />}
          {node ? "Save" : `Create ${level.noun}`}
        </Button>
      </DialogFooter>
    </form>
  )
}

type DeleteNodeDialogProps = {
  level: Level
  /** The node to delete; the dialog is open while there is one. */
  node: Organization | undefined
  onClose: () => void
  /** Deletes; rejects with the API's error, which the dialog shows. */
  onConfirm: (node: Organization) => Promise<unknown>
}

/**
 * Confirms deleting a node. The API refuses (409) while anything is under
 * it, and says what; the dialog shows that reason.
 */
export function DeleteNodeDialog({
  level,
  node,
  onClose,
  onConfirm,
}: DeleteNodeDialogProps) {
  const open = node !== undefined
  const count = useOpenCount(open)
  const shown = useLastNode(node)
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent>
        {shown && (
          <DeleteForm
            key={count}
            level={level}
            node={shown}
            onConfirm={onConfirm}
            onDone={onClose}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

function DeleteForm({
  level,
  node,
  onConfirm,
  onDone,
}: {
  level: Level
  node: Organization
  onConfirm: (node: Organization) => Promise<unknown>
  onDone: () => void
}) {
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<ApiError>()

  async function confirm() {
    setPending(true)
    setError(undefined)
    try {
      await onConfirm(node)
      onDone()
    } catch (caught) {
      setError(toApiError(caught))
    } finally {
      setPending(false)
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>Delete {node.name}?</DialogTitle>
        <DialogDescription>
          This deletes the {level.noun} and revokes every role assigned in it.
          It can't be undone.
        </DialogDescription>
      </DialogHeader>
      {error && (
        <ErrorCallout title={`Couldn't delete the ${level.noun}`}>
          {error.message}
        </ErrorCallout>
      )}
      <DialogFooter>
        <DialogClose render={<Button variant="outline" />}>Cancel</DialogClose>
        <Button variant="destructive" disabled={pending} onClick={confirm}>
          {pending && <Spinner data-icon="inline-start" />}
          Delete {level.noun}
        </Button>
      </DialogFooter>
    </>
  )
}
