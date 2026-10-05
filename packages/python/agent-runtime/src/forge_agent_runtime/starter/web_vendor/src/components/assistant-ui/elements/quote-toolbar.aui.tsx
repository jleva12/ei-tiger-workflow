import { SelectionToolbarPrimitive, useAui } from "@assistant-ui/react"
import { QuoteDownIcon } from "@hugeicons/core-free-icons"

import { Icon } from "@/components/forge/icon"

/** The composer's input, to focus after quoting. */
const composerInput = () =>
  document.querySelector<HTMLTextAreaElement>(
    'textarea[aria-label="Message input"]'
  )

/**
 * Puts `text` at the top of the composer as a Markdown quote (`> …`), above
 * anything already typed, and moves the cursor to the end to ask about it.
 * A quote is part of the message's text, so the agent reads it (the ADK
 * runtime sends only the text) and it's still there after a reload.
 */
function quoteIntoComposer(aui: ReturnType<typeof useAui>, text: string) {
  const quote = text
    .split("\n")
    .map((line) => `> ${line}`.trimEnd())
    .join("\n")
  const typed = aui.composer.getState().text.trim()
  aui.composer.setText(`${quote}\n\n${typed}`)
  requestAnimationFrame(() => {
    const input = composerInput()
    if (!input) return
    input.focus()
    input.setSelectionRange(input.value.length, input.value.length)
  })
}

/**
 * Floats over text selected in a message: "Quote" puts the selection in the
 * composer to ask about it. Render it once, inside the thread.
 */
export function QuoteToolbar() {
  const aui = useAui()
  return (
    <SelectionToolbarPrimitive.Root className="z-50 flex animate-in items-center rounded-lg border border-border bg-popover p-0.5 text-popover-foreground shadow-md duration-150 zoom-in-95 fade-in">
      <SelectionToolbarPrimitive.Quote
        className="flex h-7 items-center gap-1.5 rounded-md px-2.5 text-xs font-medium transition-colors hover:bg-muted"
        onClick={(event) => {
          // In place of the runtime's own quote, which the ADK runtime
          // doesn't send: the selection goes into the message's text.
          event.preventDefault()
          const text = window.getSelection()?.toString().trim()
          if (!text) return
          quoteIntoComposer(aui, text)
          window.getSelection()?.removeAllRanges()
        }}
      >
        <Icon icon={QuoteDownIcon} size={13} />
        Quote
      </SelectionToolbarPrimitive.Quote>
    </SelectionToolbarPrimitive.Root>
  )
}
