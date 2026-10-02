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
import { Spinner } from "@/components/ui/spinner"
import { toApiError, type ApiError } from "@/lib/api/index"
import { useLastNode, useOpenCount } from "./dialog-state"

type Copy = { title: string; description: string; confirmLabel: string }

type DeleteDialogProps = Copy & {
  open: boolean
  onClose: () => void
  /** Deletes; rejects with the API's error, which the dialog shows. */
  onConfirm: () => Promise<unknown>
}

/** Confirms a destructive action, showing why it failed if it does. */
export function DeleteDialog({
  open,
  onClose,
  onConfirm,
  title,
  description,
  confirmLabel,
}: DeleteDialogProps) {
  const count = useOpenCount(open)
  // One object per wording, so keeping the last one settles.
  const copy = React.useMemo(
    () => ({ title, description, confirmLabel }),
    [title, description, confirmLabel]
  )
  // Keep the words while the dialog animates out and its target is gone.
  const shown = useLastNode(open ? copy : undefined)
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent>
        {shown && (
          <ConfirmForm
            key={count}
            copy={shown}
            onConfirm={onConfirm}
            onDone={onClose}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

function ConfirmForm({
  copy,
  onConfirm,
  onDone,
}: {
  copy: Copy
  onConfirm: () => Promise<unknown>
  onDone: () => void
}) {
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<ApiError>()

  async function confirm() {
    setPending(true)
    setError(undefined)
    try {
      await onConfirm()
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
        <DialogTitle>{copy.title}</DialogTitle>
        <DialogDescription>{copy.description}</DialogDescription>
      </DialogHeader>
      {error && (
        <ErrorCallout title="That didn't work">{error.message}</ErrorCallout>
      )}
      <DialogFooter>
        <DialogClose render={<Button variant="outline" />}>Cancel</DialogClose>
        <Button variant="destructive" disabled={pending} onClick={confirm}>
          {pending && <Spinner data-icon="inline-start" />}
          {copy.confirmLabel}
        </Button>
      </DialogFooter>
    </>
  )
}
