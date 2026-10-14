import * as React from "react"

import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Kbd } from "@/components/ui/kbd"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import { toast } from "@/components/ui/toast"
import { toApiError, type ApiError } from "@/lib/api/index"
import { useOpenCount } from "@/features/admin/components/dialog-state"
import { useStartCreator, type SkillRecord } from "@/features/skills/lib/api"

const EXAMPLES = [
  "Triage new support tickets: tag them by product area and urgency, and open a GitHub issue for bugs.",
  "Write release notes from the merged pull requests, in our changelog's format.",
  "Answer questions about our leave and expense policies from the HR knowledge base, citing the policy.",
]

/**
 * The AI skill creator: the person describes a skill, and an agent that
 * knows Forge writes it in the background, looking at what the organization
 * has (its MCP servers' tools, knowledge bases, workflows, agents and
 * credentials) so the skill names real things. It's added to the vault
 * when it's done; the vault shows it working meanwhile. Given a skill, it
 * reworks that one instead.
 */
export function SkillCreatorDialog({
  open,
  onOpenChange,
  organizationId,
  revising,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  organizationId: string
  /** The skill to rework; none for a new one. */
  revising?: SkillRecord
}) {
  const count = useOpenCount(open)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <CreatorForm
          key={count}
          organizationId={organizationId}
          revising={revising}
          onDone={() => onOpenChange(false)}
        />
      </DialogContent>
    </Dialog>
  )
}

function CreatorForm({
  organizationId,
  revising,
  onDone,
}: {
  organizationId: string
  revising?: SkillRecord
  onDone: () => void
}) {
  const [brief, setBrief] = React.useState("")
  const [error, setError] = React.useState<ApiError>()
  const start = useStartCreator(organizationId)
  const id = React.useId()

  async function submit(event?: React.SubmitEvent<HTMLFormElement>) {
    event?.preventDefault()
    if (!brief.trim()) return
    setError(undefined)
    try {
      await start.mutateAsync({
        brief: brief.trim(),
        revises: revising?.id ?? null,
      })
      toast.add({
        title: revising
          ? `The skill creator is reworking ${revising.name}`
          : "The skill creator is writing your skill",
        description:
          "It's added to the vault when it's done: you can carry on meanwhile.",
        type: "info",
      })
      onDone()
    } catch (caught) {
      setError(toApiError(caught))
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-5">
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2">
          <Icon icon="sparkles" />
          {revising ? `Rework ${revising.name} with AI` : "AI skill creator"}
        </DialogTitle>
        <DialogDescription>
          {revising
            ? "Say what to change. The creator rewrites the skill in the background and saves it in its place."
            : "Describe what the skill should do. An agent that knows Forge looks at what your organization has (MCP servers and their tools, knowledge bases, workflows, agents, credentials) and writes the skill in the background."}
        </DialogDescription>
      </DialogHeader>
      <Field>
        <FieldLabel htmlFor={id}>
          {revising ? "What to change" : "What the skill does"}
        </FieldLabel>
        <Textarea
          id={id}
          autoFocus
          rows={6}
          value={brief}
          maxLength={8000}
          placeholder={
            revising
              ? "Also check the customer's plan before tagging, and hand back a one-line summary."
              : EXAMPLES[0]
          }
          onChange={(e) => setBrief(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault()
              void submit()
            }
          }}
        />
        <FieldDescription>
          Say when it should be used, the steps, and what it hands back.{" "}
          <Kbd>⌘</Kbd> <Kbd>Enter</Kbd> starts it.
        </FieldDescription>
      </Field>
      {!revising && (
        <div className="flex flex-col gap-1.5">
          <span className="text-2xs text-subtle">Try one of these</span>
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              className="rounded-(--radius-item) border px-2.5 py-1.5 text-left text-2xs/[1.5] text-muted-foreground hover:bg-muted hover:text-foreground"
              onClick={() => setBrief(example)}
            >
              {example}
            </button>
          ))}
        </div>
      )}
      {error && (
        <ErrorCallout title="Couldn't start the skill creator">
          {error.message}
        </ErrorCallout>
      )}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onDone}>
          Cancel
        </Button>
        <Button type="submit" disabled={!brief.trim() || start.isPending}>
          {start.isPending ? (
            <Spinner data-icon="inline-start" />
          ) : (
            <Icon icon="sparkles" data-icon="inline-start" />
          )}
          {revising ? "Rework it" : "Create skill"}
        </Button>
      </DialogFooter>
    </form>
  )
}
