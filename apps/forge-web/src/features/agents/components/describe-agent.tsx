import * as React from "react"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { Field, FieldLabel } from "@/components/ui/field"
import { Textarea } from "@/components/ui/textarea"
import { askAssistant } from "@/features/assistant/lib/assistant-asks"

/*
 * Over a new agent's empty canvas: the person says what the agent should do,
 * and the Forge assistant builds it here (forge-admin-api
 * `assistant/agent_tools.py`): it proposes how the agent will work, then
 * adds its tools and helpers and writes its instructions, each change shown
 * on the canvas as one step undo takes back.
 */

/** What the assistant is sent: the person's words, and what to do with them. */
function describeAgentPrompt(description: string) {
  return [
    "Build this agent with me. Here's what it should do:",
    "",
    description.trim(),
    "",
    "Propose how it will work in plain words, then build it here with its tools and instructions.",
  ].join("\n")
}

export function DescribeAgentCard({ onDismiss }: { onDismiss: () => void }) {
  const id = React.useId()
  const [description, setDescription] = React.useState("")
  const ready = description.trim().length > 0
  const submit = () => {
    if (!ready) return
    askAssistant(describeAgentPrompt(description))
    onDismiss()
  }
  return (
    <div className="pointer-events-none absolute inset-x-0 top-4 z-10 flex justify-center px-3">
      <form
        aria-labelledby={`${id}-title`}
        className="pointer-events-auto flex w-[min(34rem,100%)] flex-col gap-3 rounded-(--radius-card) border bg-background p-4 shadow-(--shadow-float)"
        onSubmit={(event) => {
          event.preventDefault()
          submit()
        }}
      >
        <div className="flex items-start gap-2.5">
          <Icon
            icon="sparkles"
            size={16}
            className="mt-0.5 shrink-0 text-muted-foreground"
          />
          <div className="min-w-0 flex-1">
            <h2 id={`${id}-title`} className="text-sm font-medium">
              Describe your agent
            </h2>
            <p className="mt-0.5 text-xs text-muted-foreground">
              Say what it should do and who it&apos;s for. The assistant builds
              it here: its tools, helpers and instructions. You see each change,
              and can undo it.
            </p>
          </div>
          <Button
            type="button"
            size="icon-xs"
            variant="ghost"
            aria-label="Close"
            onClick={onDismiss}
          >
            <Icon icon="close" />
          </Button>
        </div>
        <Field>
          <FieldLabel htmlFor={`${id}-description`} className="sr-only">
            What should it do?
          </FieldLabel>
          <Textarea
            id={`${id}-description`}
            rows={3}
            value={description}
            placeholder="E.g. Answer our staff's questions about HR policies from the HR handbook, and open a helpdesk ticket when it can't help."
            onChange={(event) => setDescription(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                event.preventDefault()
                submit()
              }
            }}
          />
        </Field>
        <div className="flex items-center justify-end gap-2">
          <Button type="button" variant="ghost" size="sm" onClick={onDismiss}>
            I&apos;ll build it myself
          </Button>
          <Button type="submit" size="sm" disabled={!ready}>
            <Icon icon="sparkles" data-icon="inline-start" />
            Build it with the assistant
          </Button>
        </div>
      </form>
    </div>
  )
}
