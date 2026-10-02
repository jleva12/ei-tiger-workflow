import * as React from "react"
import { useAuiEvent } from "@assistant-ui/react"
import { Alert02Icon } from "@hugeicons/core-free-icons"

import { Icon } from "@/components/forge/icon"

const SHOW_MS = 6000

/**
 * Above the composer's input, for a few seconds: why a file just picked or
 * dropped wasn't attached (its type isn't accepted, or the adapter refused
 * it). The runtime turns those files away before they get a chip, and says
 * so only with an event.
 */
export function AttachmentNotice() {
  const [notice, setNotice] = React.useState<{ id: number; text: string }>()
  useAuiEvent(
    "composer.attachmentAddError",
    ({ reason, message, contentType }) => {
      const text =
        reason === "not-accepted"
          ? `That file${contentType ? ` (${contentType})` : ""} can't be attached. Attach images, PDFs, or text and code files.`
          : message
      setNotice({ id: Date.now(), text })
    }
  )
  React.useEffect(() => {
    if (!notice) return
    const timer = setTimeout(() => setNotice(undefined), SHOW_MS)
    return () => clearTimeout(timer)
  }, [notice])
  if (!notice) return null
  return (
    <p
      key={notice.id}
      role="status"
      className="mx-1.5 mt-1 flex animate-in items-center gap-2 rounded-md bg-warning-surface px-2.5 py-1.5 text-xs text-warning-foreground duration-200 fade-in slide-in-from-bottom-1"
    >
      <Icon icon={Alert02Icon} size={13} className="shrink-0" />
      {notice.text}
    </p>
  )
}
