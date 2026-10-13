import * as React from "react"

import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button"
import { Icon } from "@/components/forge/icon"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Kbd } from "@/components/ui/kbd"
import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverHeader,
  PopoverTitle,
  PopoverTrigger,
} from "@/components/ui/popover"
import { Spinner } from "@/components/ui/spinner"
import { Textarea } from "@/components/ui/textarea"
import { ApiError } from "@/lib/api/index"
import {
  useWriteInstruction,
  type InstructionBrief,
  type InstructionDraft,
} from "@/features/agents/lib/instruction-writer"

/*
 * The ✨ beside an agent's instruction: the person says what the agent
 * should do, and the admin API's instruction writer writes the instruction
 * from that and the agent as it's set up (its tools, hand-offs and the data
 * it can put in). The draft is shown before it goes in; asking again with
 * a draft changes the draft. Using it is one step of undo.
 */

type Props = {
  organizationId: string
  /** The field's instruction now. */
  value: string
  /** Puts the draft in the field. */
  onApply: (instruction: string) => void
  /** The agent as it's set up now, read when the ask is sent. */
  brief: () => Promise<InstructionBrief> | InstructionBrief
}

export function InstructionWriter({
  organizationId,
  value,
  onApply,
  brief,
}: Props) {
  const [open, setOpen] = React.useState(false)
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <TooltipIconButton
            tooltip="Write it with AI"
            side="top"
            // The editor keeps its cursor and focus until the popover takes it.
            onMouseDown={(event) => event.preventDefault()}
            className="-my-1 size-6 text-muted-foreground hover:text-foreground"
          />
        }
      >
        <Icon icon="sparkles" size={15} />
      </PopoverTrigger>
      <PopoverContent
        align="end"
        side="bottom"
        className="max-h-[min(40rem,80vh)] w-[28rem] max-w-[calc(100vw-2rem)] overflow-y-auto"
        onKeyDown={(event) => {
          // Escape closes the writer alone, not the settings it's in
          // (they leave an Escape that's been handled).
          if (event.key !== "Escape") return
          event.preventDefault()
          setOpen(false)
        }}
      >
        {open && (
          <WriterBody
            organizationId={organizationId}
            value={value}
            brief={brief}
            onApply={(instruction) => {
              onApply(instruction)
              setOpen(false)
            }}
          />
        )}
      </PopoverContent>
    </Popover>
  )
}

function WriterBody({
  organizationId,
  value,
  onApply,
  brief,
}: Omit<Props, "onApply"> & { onApply: (instruction: string) => void }) {
  const write = useWriteInstruction(organizationId)
  const [ask, setAsk] = React.useState("")
  const [draft, setDraft] = React.useState<InstructionDraft | null>(null)
  const [failed, setFailed] = React.useState<string | null>(null)
  const [writing, setWriting] = React.useState(false)
  // The ask being written: one at a time, even when a second click comes
  // before the first has rendered.
  const abort = React.useRef<AbortController | null>(null)
  const askId = React.useId()
  const starting = value.trim()

  // A closed popover stops the writer.
  React.useEffect(() => () => abort.current?.abort(), [])

  const send = async () => {
    const wanted = ask.trim()
    if (!wanted || abort.current) return
    const controller = new AbortController()
    abort.current = controller
    setFailed(null)
    setWriting(true)
    try {
      const request = {
        ask: wanted,
        // A draft is changed; otherwise the field's instruction, when it has one.
        current: draft?.instruction ?? starting,
        brief: await brief(),
      }
      if (controller.signal.aborted) return
      const written = await write.mutateAsync({
        request,
        signal: controller.signal,
      })
      if (controller.signal.aborted) return
      setDraft(written)
      setAsk("")
    } catch (error) {
      if (controller.signal.aborted) return
      setFailed(
        error instanceof ApiError || error instanceof Error
          ? error.message
          : "The instruction couldn't be written."
      )
    } finally {
      if (abort.current === controller) {
        abort.current = null
        setWriting(false)
      }
    }
  }

  const stop = () => {
    abort.current?.abort()
    abort.current = null
    setWriting(false)
  }

  return (
    <>
      <PopoverHeader>
        <PopoverTitle className="flex items-center gap-1.5 text-sm">
          <Icon icon="sparkles" size={15} />
          Write the instruction
        </PopoverTitle>
        <PopoverDescription className="text-xs/[1.5]">
          {draft
            ? "Use it, or say what to change."
            : starting
              ? "Say what the agent should do, or what to change: the current instruction is reworked, with its tools, hand-offs and data."
              : "Say what the agent should do. It's written for the agent as it's set up: its tools, who it hands off to and the data it can put in."}
        </PopoverDescription>
      </PopoverHeader>

      {draft && (
        <div className="flex flex-col gap-2">
          <pre
            tabIndex={0}
            aria-label="The written instruction"
            className="max-h-72 overflow-y-auto rounded-(--radius-control) border border-border bg-muted/50 px-2.5 py-2 font-sans text-xs/[1.6] whitespace-pre-wrap text-foreground"
          >
            {draft.instruction}
          </pre>
          {draft.notes.length > 0 && (
            <ul className="flex flex-col gap-1 text-2xs/[1.5] text-muted-foreground">
              {draft.notes.map((note) => (
                <li key={note} className="flex gap-1.5">
                  <Icon
                    icon="info"
                    size={12}
                    className="mt-0.5 shrink-0 text-subtle"
                  />
                  {note}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <form
        className="flex flex-col gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          void send()
        }}
      >
        <label htmlFor={askId} className="sr-only">
          {draft ? "What to change" : "What the agent should do"}
        </label>
        <Textarea
          id={askId}
          autoFocus
          value={ask}
          disabled={writing}
          rows={3}
          className="max-h-40 min-h-16 text-xs"
          placeholder={
            draft
              ? "Shorter, and have it confirm before refunding."
              : "Answers customers' questions about their orders: looks the order up, explains refunds, and hands billing disputes to Billing."
          }
          onChange={(event) => setAsk(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
              event.preventDefault()
              void send()
            }
          }}
        />
        {failed && (
          <Alert variant="error" className="px-3 py-2 text-xs">
            <AlertDescription>{failed}</AlertDescription>
          </Alert>
        )}
        <div className="flex items-center justify-between gap-2">
          <span className="text-2xs text-subtle">
            {writing ? (
              "Writing… this can take a little while."
            ) : draft ? (
              <>Written by {draft.model}</>
            ) : (
              <>
                <Kbd>⌘</Kbd> <Kbd>Enter</Kbd> to send
              </>
            )}
          </span>
          <div className="flex items-center gap-1.5">
            {writing && (
              <Spinner className="size-4 text-muted-foreground" aria-hidden />
            )}
            {writing ? (
              <Button type="button" variant="outline" size="sm" onClick={stop}>
                Stop
              </Button>
            ) : (
              <Button
                type="submit"
                variant={draft ? "outline" : "default"}
                size="sm"
                disabled={!ask.trim()}
              >
                <Icon icon="sparkles" data-icon="inline-start" />
                {draft ? "Change it" : "Write it"}
              </Button>
            )}
            {draft && !writing && (
              <Button
                type="button"
                size="sm"
                onClick={() => onApply(draft.instruction)}
              >
                <Icon icon="check" data-icon="inline-start" />
                {starting ? "Replace instruction" : "Use it"}
              </Button>
            )}
          </div>
        </div>
      </form>
    </>
  )
}
