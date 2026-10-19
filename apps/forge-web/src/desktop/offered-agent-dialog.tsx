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
import { Skeleton } from "@/components/ui/skeleton"
import { useChatPageInfo } from "@/features/agents/lib/chat-page"
import type { DesktopAgentLink } from "@/features/agents/lib/desktop-link"

import { failureOf } from "./failures"
import type { DesktopAgent } from "./library"
import { runtimeOf } from "./links"

/**
 * Asks before adding the agent a link to the app offers (a chat page's
 * "Open in the desktop app"): any website can open such a link, so the
 * person sees the agent and the server it's on, and adds it themselves.
 */
export function OfferedAgentDialog({
  link,
  onAdd,
  onClose,
}: {
  /** The agent offered; none closes the dialog. */
  link: DesktopAgentLink | undefined
  onAdd: (agent: DesktopAgent) => void
  onClose: () => void
}) {
  return (
    <Dialog open={Boolean(link)} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        {link && (
          <Offer
            key={`${link.server} ${link.token}`}
            link={link}
            onAdd={onAdd}
          />
        )}
      </DialogContent>
    </Dialog>
  )
}

function Offer({
  link,
  onAdd,
}: {
  link: DesktopAgentLink
  onAdd: (agent: DesktopAgent) => void
}) {
  const info = useChatPageInfo(link.token, runtimeOf(link.server))
  const host = new URL(link.server).host
  return (
    <div className="grid gap-6">
      <DialogHeader>
        <DialogTitle>Add this agent?</DialogTitle>
        <DialogDescription>
          A link asked Forge Agents to add an agent. Add it if you trust where
          the link came from.
        </DialogDescription>
      </DialogHeader>
      {info.error ? (
        <ErrorCallout title="The link's agent doesn't answer">
          {failureOf(info.error, link.server)}
        </ErrorCallout>
      ) : (
        <div className="flex flex-col gap-3 rounded-(--radius-card) border bg-muted/40 p-3">
          {info.data ? (
            <div className="flex flex-col gap-1">
              <p className="text-sm font-medium text-foreground">
                {info.data.name}
              </p>
              {info.data.description && (
                <p className="line-clamp-3 text-xs text-muted-foreground">
                  {info.data.description}
                </p>
              )}
            </div>
          ) : (
            <div className="flex flex-col gap-2" aria-busy="true">
              <Skeleton className="h-4 w-40" />
              <Skeleton className="h-3 w-64 max-w-full" />
            </div>
          )}
          <div className="flex flex-col gap-0.5 border-t pt-3">
            <p className="text-2xs font-medium text-muted-foreground">
              On the server
            </p>
            <p className="text-xs font-medium text-foreground">{host}</p>
            <p className="truncate font-mono text-3xs text-subtle">
              {link.server}
            </p>
          </div>
        </div>
      )}
      <DialogFooter>
        <DialogClose render={<Button variant="outline" />}>Cancel</DialogClose>
        <Button
          disabled={!info.data}
          onClick={() =>
            info.data &&
            onAdd({
              token: link.token,
              server: link.server,
              name: info.data.name,
              added: new Date().toISOString(),
            })
          }
        >
          Add agent
        </Button>
      </DialogFooter>
    </div>
  )
}
