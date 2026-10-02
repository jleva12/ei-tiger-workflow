import * as React from "react"
import { cn } from "cn"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { useCopyToClipboard } from "@/hooks/use-copy-to-clipboard"

// A string (and the colon after it, when it's a key), a literal, a number.
const TOKEN =
  /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g

function highlight(text: string): React.ReactNode[] {
  const parts: React.ReactNode[] = []
  let last = 0
  for (const match of text.matchAll(TOKEN)) {
    const [whole, string, colon, literal, number] = match
    const at = match.index
    if (at > last) parts.push(text.slice(last, at))
    if (string !== undefined && colon !== undefined) {
      parts.push(
        <span key={at} className="text-foreground">
          {string}
        </span>,
        colon
      )
    } else {
      const tone = string
        ? "text-tone-green-foreground"
        : literal
          ? "text-tone-pink-foreground"
          : number
            ? "text-tone-amber-foreground"
            : undefined
      parts.push(
        <span key={at} className={tone}>
          {whole}
        </span>
      )
    }
    last = at + whole.length
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts
}

/** JSON, indented and coloured, in a scrolling block. */
export function JsonView({
  value,
  className,
}: {
  value: unknown
  className?: string
}) {
  const text = React.useMemo(() => JSON.stringify(value, null, 2), [value])
  const parts = React.useMemo(() => highlight(text ?? "undefined"), [text])
  return (
    <pre
      tabIndex={0}
      className={cn(
        "overflow-auto rounded-(--radius-control) border bg-muted p-3 font-mono text-2xs/[1.6] text-muted-foreground",
        className
      )}
    >
      {parts}
    </pre>
  )
}

/** Copies text; says so for a moment after. */
export function CopyButton({
  value,
  label = "Copy",
  className,
  ...props
}: Omit<React.ComponentProps<typeof Button>, "value" | "onClick"> & {
  value: string
  label?: string
}) {
  const { isCopied, copyToClipboard } = useCopyToClipboard({
    copiedDuration: 2000,
  })
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      className={className}
      onClick={() => copyToClipboard(value)}
      {...props}
    >
      <Icon icon={isCopied ? "check" : "copy"} data-icon="inline-start" />
      {isCopied ? "Copied" : label}
    </Button>
  )
}
