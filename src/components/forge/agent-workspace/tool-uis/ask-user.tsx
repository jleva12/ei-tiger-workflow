import * as React from "react"
import type { ToolCallMessagePartComponent } from "@assistant-ui/react"
import { useAdkSubmitInput } from "@assistant-ui/react-google-adk"
import {
  MessageQuestionIcon,
  SentIcon,
  Tick02Icon,
} from "@hugeicons/core-free-icons"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { Spinner } from "@/components/ui/spinner"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"

/** The JSON schema the agent gives for the answer it wants. */
type Schema = {
  type?: string
  enum?: unknown[]
  items?: Schema
  properties?: Record<string, Schema>
  required?: string[]
  title?: string
  description?: string
}

type AskArgs = {
  message?: string
  response_schema?: Schema
  responseSchema?: Schema
}

/** What the person answers: a choice, choices, yes/no, text or a form. */
type Answer =
  | string
  | number
  | boolean
  | string[]
  | Record<string, string | number | boolean>

/** ADK's reply to the call, `{ result }`, as JSON text or a value. */
function answerOf(result: unknown): unknown {
  let value = result
  if (value === undefined || value === null) return undefined
  if (typeof value === "string") {
    try {
      value = JSON.parse(value)
    } catch {
      return value
    }
  }
  return typeof value === "object" && value !== null && "result" in value
    ? (value as { result: unknown }).result
    : value
}

const labelOf = (key: string, schema: Schema) =>
  schema.title ??
  key.replace(/_/g, " ").replace(/^\w/, (first) => first.toUpperCase())

function formatAnswer(answer: unknown, schema: Schema | undefined): string {
  if (typeof answer === "boolean") return answer ? "Yes" : "No"
  if (Array.isArray(answer)) return answer.map(String).join(", ") || "None"
  if (answer && typeof answer === "object")
    return Object.entries(answer)
      .map(
        ([key, value]) =>
          `${labelOf(key, schema?.properties?.[key] ?? {})}: ${formatAnswer(value, undefined)}`
      )
      .join(" · ")
  return String(answer)
}

/** What to ask with, from the schema's shape. */
function kindOf(schema: Schema | undefined) {
  if (schema?.enum?.length) return "choice"
  if (schema?.type === "array" && schema.items?.enum?.length) return "multi"
  if (schema?.type === "boolean") return "boolean"
  if (schema?.type === "object" && schema.properties) return "form"
  return "text"
}

function ChoiceList({
  options,
  pending,
  onPick,
}: {
  options: string[]
  pending: Answer | undefined
  onPick: (option: string) => void
}) {
  return (
    <div role="list" className="flex flex-col gap-1.5">
      {options.map((option, index) => (
        <button
          key={option}
          type="button"
          role="listitem"
          disabled={pending !== undefined}
          onClick={() => onPick(option)}
          className={cn(
            "group flex items-center gap-3 rounded-(--radius-soft) border border-border bg-background px-3 py-2 text-start text-sm transition-[border-color,background-color,transform] duration-150",
            "hover:border-foreground/25 hover:bg-muted/60 focus-visible:ring-2 focus-visible:ring-ring/40 focus-visible:outline-none active:scale-[0.99] disabled:opacity-60",
            pending === option &&
              "border-foreground/40 bg-muted/60 opacity-100!"
          )}
        >
          <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-muted text-2xs text-muted-foreground tabular-nums transition-colors group-hover:bg-foreground group-hover:text-background">
            {pending === option ? <Spinner className="size-3" /> : index + 1}
          </span>
          <span className="min-w-0 flex-1">{option}</span>
        </button>
      ))}
    </div>
  )
}

function MultiChoice({
  options,
  pending,
  onSubmit,
}: {
  options: string[]
  pending: boolean
  onSubmit: (picked: string[]) => void
}) {
  const [picked, setPicked] = React.useState<string[]>([])
  const toggle = (option: string, on: boolean) =>
    setPicked((current) =>
      on
        ? options.filter((each) => each === option || current.includes(each))
        : current.filter((each) => each !== option)
    )
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        {options.map((option) => (
          <label
            key={option}
            className="flex cursor-pointer items-center gap-3 rounded-(--radius-soft) border border-border bg-background px-3 py-2 text-sm transition-colors hover:bg-muted/60 has-data-checked:border-foreground/30 has-data-checked:bg-muted/60"
          >
            <Checkbox
              checked={picked.includes(option)}
              onCheckedChange={(on) => toggle(option, on)}
              disabled={pending}
            />
            {option}
          </label>
        ))}
      </div>
      <div className="flex items-center gap-2">
        <Button size="sm" disabled={pending} onClick={() => onSubmit(picked)}>
          {pending ? (
            <Spinner data-icon="inline-start" />
          ) : (
            <Icon icon={Tick02Icon} data-icon="inline-start" />
          )}
          {picked.length ? `Submit ${picked.length}` : "Submit none"}
        </Button>
        <span className="text-xs text-muted-foreground">Pick any</span>
      </div>
    </div>
  )
}

function TextAnswer({
  pending,
  onSubmit,
}: {
  pending: boolean
  onSubmit: (text: string) => void
}) {
  const [text, setText] = React.useState("")
  const send = () => text.trim() && onSubmit(text.trim())
  return (
    <form
      className="flex flex-col gap-2"
      onSubmit={(event) => {
        event.preventDefault()
        send()
      }}
    >
      <Textarea
        value={text}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault()
            send()
          }
        }}
        placeholder="Type your answer…"
        aria-label="Your answer"
        rows={2}
        disabled={pending}
        className="min-h-16 resize-none bg-background text-sm"
      />
      <div className="flex items-center gap-2">
        <Button type="submit" size="sm" disabled={pending || !text.trim()}>
          {pending ? (
            <Spinner data-icon="inline-start" />
          ) : (
            <Icon icon={SentIcon} data-icon="inline-start" />
          )}
          Send
        </Button>
        <span className="text-xs text-muted-foreground">
          Enter to send, Shift+Enter for a new line
        </span>
      </div>
    </form>
  )
}

function FormAnswer({
  schema,
  pending,
  onSubmit,
}: {
  schema: Schema
  pending: boolean
  onSubmit: (values: Record<string, string | number | boolean>) => void
}) {
  // Only real field schemas: a model may list `required` among them.
  const fields = Object.entries(schema.properties ?? {}).filter(
    ([, field]) => typeof field === "object" && !Array.isArray(field)
  )
  const [values, setValues] = React.useState<Record<string, string | boolean>>(
    () =>
      Object.fromEntries(
        fields.map(([key, field]) => [
          key,
          field.type === "boolean"
            ? false
            : field.enum
              ? String(field.enum[0])
              : "",
        ])
      )
  )
  const set = (key: string, value: string | boolean) =>
    setValues((current) => ({ ...current, [key]: value }))
  const missing = (schema.required ?? []).filter(
    (key) => values[key] === "" || values[key] === undefined
  )
  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(event) => {
        event.preventDefault()
        if (missing.length) return
        onSubmit(
          Object.fromEntries(
            fields.flatMap(
              ([key, field]): [string, string | number | boolean][] => {
                const value = values[key]
                if (value === "") return []
                if (field.type === "number" || field.type === "integer")
                  return [[key, Number(value)]]
                return [[key, value]]
              }
            )
          )
        )
      }}
    >
      <div className="grid gap-3 sm:grid-cols-2">
        {fields.map(([key, field]) => {
          const label = labelOf(key, field)
          const required = schema.required?.includes(key)
          return (
            <label
              key={key}
              className={cn(
                "flex flex-col gap-1.5 text-xs",
                field.type === "boolean" &&
                  "flex-row items-center justify-between self-end rounded-(--radius-soft) border border-border bg-background px-3 py-2"
              )}
            >
              <span className="font-medium text-foreground">
                {label}
                {required && <span className="text-muted-foreground"> *</span>}
              </span>
              {field.type === "boolean" ? (
                <Switch
                  checked={Boolean(values[key])}
                  onCheckedChange={(on) => set(key, on)}
                  disabled={pending}
                />
              ) : field.enum ? (
                <NativeSelect
                  value={String(values[key])}
                  onChange={(event) => set(key, event.target.value)}
                  disabled={pending}
                >
                  {field.enum.map((option) => (
                    <NativeSelectOption
                      key={String(option)}
                      value={String(option)}
                    >
                      {String(option)}
                    </NativeSelectOption>
                  ))}
                </NativeSelect>
              ) : (
                <Input
                  type={
                    field.type === "number" || field.type === "integer"
                      ? "number"
                      : "text"
                  }
                  value={String(values[key] ?? "")}
                  onChange={(event) => set(key, event.target.value)}
                  placeholder={field.description}
                  disabled={pending}
                  className="h-8 bg-background text-sm"
                />
              )}
            </label>
          )
        })}
      </div>
      <div>
        <Button
          type="submit"
          size="sm"
          disabled={pending || missing.length > 0}
        >
          {pending ? (
            <Spinner data-icon="inline-start" />
          ) : (
            <Icon icon={SentIcon} data-icon="inline-start" />
          )}
          Send
        </Button>
      </div>
    </form>
  )
}

/**
 * ADK's `adk_request_input` call (chat-api: `request_input`): the agent's
 * question, with an answer control that fits its `response_schema` (a list
 * of choices, checkboxes, yes/no, a small form, or free text). The run waits
 * until the person answers; the card then folds to their answer.
 */
export const AskUserUI: ToolCallMessagePartComponent<AskArgs> = ({
  toolCallId,
  args,
  result,
}) => {
  const submitInput = useAdkSubmitInput()
  const [pending, setPending] = React.useState<Answer>()
  const [error, setError] = React.useState<string>()
  const schema = args.response_schema ?? args.responseSchema
  const question = args.message ?? "The assistant needs your input to continue."
  const answer = answerOf(result)

  if (answer !== undefined)
    return (
      <div
        data-slot="ask-user-answered"
        className="my-1.5 flex animate-in flex-col gap-1 duration-300 fade-in"
      >
        <p className="flex items-center gap-2 text-xs text-muted-foreground">
          <Icon icon={MessageQuestionIcon} size={13} />
          {question}
        </p>
        <p className="flex items-center gap-2 ps-5 text-sm">
          <span className="flex size-4 shrink-0 items-center justify-center rounded-full bg-success-surface text-success-foreground">
            <Icon icon={Tick02Icon} size={10} />
          </span>
          <span className="text-foreground">
            {formatAnswer(answer, schema)}
          </span>
        </p>
      </div>
    )

  const submit = async (value: Answer) => {
    setPending(value)
    setError(undefined)
    try {
      await submitInput(toolCallId, value)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
      setPending(undefined)
    }
  }
  const kind = kindOf(schema)
  const busy = pending !== undefined

  return (
    <section
      data-slot="ask-user"
      aria-label="Question for you"
      className="my-2 animate-in overflow-hidden rounded-(--radius-card) border border-border bg-card shadow-sm duration-300 fade-in slide-in-from-bottom-1"
    >
      <header className="flex items-center gap-2 border-b bg-muted/50 px-3.5 py-2 text-xs text-muted-foreground">
        <Icon icon={MessageQuestionIcon} size={14} />
        <span className="font-medium text-foreground">Question for you</span>
        <span className="ms-auto flex items-center gap-1.5">
          <span aria-hidden className="relative flex size-1.5">
            <span className="absolute inline-flex size-full animate-ping rounded-full bg-foreground/60 opacity-60 motion-reduce:animate-none" />
            <span className="relative inline-flex size-1.5 rounded-full bg-foreground/70" />
          </span>
          Waiting for you
        </span>
      </header>
      <div className="flex flex-col gap-3 p-3.5">
        <p className="text-sm font-medium whitespace-pre-line text-foreground">
          {question}
        </p>
        {kind === "choice" && (
          <ChoiceList
            options={(schema?.enum ?? []).map(String)}
            pending={pending}
            onPick={submit}
          />
        )}
        {kind === "multi" && (
          <MultiChoice
            options={(schema?.items?.enum ?? []).map(String)}
            pending={busy}
            onSubmit={submit}
          />
        )}
        {kind === "boolean" && (
          <div className="flex gap-2">
            {[true, false].map((choice) => (
              <Button
                key={String(choice)}
                size="sm"
                variant={choice ? "default" : "outline"}
                disabled={busy}
                onClick={() => submit(choice)}
              >
                {pending === choice && <Spinner data-icon="inline-start" />}
                {choice ? "Yes" : "No"}
              </Button>
            ))}
          </div>
        )}
        {kind === "form" && schema && (
          <FormAnswer schema={schema} pending={busy} onSubmit={submit} />
        )}
        {kind === "text" && <TextAnswer pending={busy} onSubmit={submit} />}
        {error && <p className="text-xs text-destructive">{error}</p>}
      </div>
    </section>
  )
}
