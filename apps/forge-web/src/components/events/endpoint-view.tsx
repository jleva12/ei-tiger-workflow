import * as React from "react"
import { cn } from "cn"

import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Chip, ConnectionDot } from "@/components/forge/status"
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
import { Spinner } from "@/components/ui/spinner"
import {
  curlExample,
  EVENTS_ICON,
  useEndpointActions,
  useEventEndpoint,
  type EventEndpoint,
} from "@/lib/events"
import { CopyButton } from "./json-view"
import { SendingGuide } from "./sending-guide"

/**
 * The Events page's Endpoint tab: the organization's inbound endpoint on the left
 * (whether it's on, its URL, its token) and how to send it events on the
 * right, divided by a hairline; stacked on narrow shells. Whoever manages
 * the organization's events turns it on (the first time makes it and shows its
 * token, once), turns it off, or replaces the token.
 */
export function EndpointView({
  organizationId,
  organizationName,
  canManage,
  exampleKey,
}: {
  organizationId: string
  organizationName: string
  /** `events:manage` in the organization. */
  canManage: boolean
  /** An event type's key, for the example request. */
  exampleKey?: string
}) {
  const endpoint = useEventEndpoint(organizationId)
  const current = endpoint.data ?? undefined

  return (
    <div className="mx-auto grid w-full max-w-[1180px] grid-cols-[minmax(0,5fr)_minmax(0,6fr)] pt-3 pb-10 @max-[1050px]/shell:grid-cols-1">
      <div className="min-w-0 pr-12 @max-[1050px]/shell:pr-0 @max-[1050px]/shell:pb-9">
        {endpoint.error ? (
          <ErrorCallout
            title="Couldn't load the endpoint"
            action={
              <Button
                variant="outline"
                size="sm"
                onClick={() => void endpoint.refetch()}
              >
                Retry
              </Button>
            }
          >
            {endpoint.error.message}
          </ErrorCallout>
        ) : endpoint.isPending ? (
          <div className="flex flex-col gap-4" aria-busy="true">
            <Skeleton className="h-5 w-32" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-28 w-full" />
          </div>
        ) : (
          <EndpointDetails
            organizationId={organizationId}
            organizationName={organizationName}
            canManage={canManage}
            exampleKey={exampleKey}
            endpoint={current}
          />
        )}
      </div>
      <SendingGuide
        url={current?.url}
        eventKey={exampleKey}
        className="border-l pl-12 @max-[1050px]/shell:border-t @max-[1050px]/shell:border-l-0 @max-[1050px]/shell:pt-9 @max-[1050px]/shell:pl-0"
      />
    </div>
  )
}

function EndpointDetails({
  organizationId,
  organizationName,
  canManage,
  exampleKey,
  endpoint,
}: {
  organizationId: string
  organizationName: string
  canManage: boolean
  exampleKey?: string
  /** None before it's turned on the first time. */
  endpoint: EventEndpoint | undefined
}) {
  const { toggle, rotate } = useEndpointActions(organizationId)
  // The answer that carries a token, shown once.
  const [revealed, setRevealed] = React.useState<EventEndpoint>()
  const [confirming, setConfirming] = React.useState(false)
  const turn = (enabled: boolean) =>
    toggle.mutate(enabled, {
      onSuccess: (answer) => {
        if (answer.token) setRevealed(answer)
      },
    })

  return (
    <section aria-labelledby="endpoint-title" className="flex flex-col gap-5">
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <h2 id="endpoint-title" className="text-sm font-medium">
            Endpoint
          </h2>
          {endpoint &&
            (endpoint.enabled ? (
              <Chip tone="success">On</Chip>
            ) : (
              <Chip tone="neutral">Off</Chip>
            ))}
        </div>
        <p className="max-w-[62ch] text-[0.8125rem] text-muted-foreground">
          Incident managers, defect trackers and pipelines post{" "}
          {organizationName}'s events here. Each one is kept and checked against
          its type's schema.
        </p>
      </div>

      {!endpoint ? (
        <div className="flex flex-col items-start gap-4 rounded-(--radius-card) border border-dashed px-5 py-5">
          <span className="flex size-9 items-center justify-center rounded-(--radius-card) border bg-background text-muted-foreground">
            <Icon icon={EVENTS_ICON} size={18} />
          </span>
          <p className="max-w-[46ch] text-[0.8125rem] text-muted-foreground">
            {canManage
              ? "Not on yet. Turning it on gives senders a URL and a token, which is shown once. Event types still accept nothing until each is turned on."
              : `Not on yet. One of ${organizationName}'s admins turns it on.`}
          </p>
          {canManage && (
            <Button
              variant="outline"
              disabled={toggle.isPending}
              onClick={() => turn(true)}
            >
              {toggle.isPending && <Spinner data-icon="inline-start" />}
              Turn on the endpoint
            </Button>
          )}
        </div>
      ) : (
        <dl className="grid grid-cols-[5.5rem_minmax(0,1fr)] border-t text-xs">
          <Fact label="Status">
            <span className="flex min-w-0 flex-1 items-center gap-2">
              <ConnectionDot online={endpoint.enabled} />
              <span className="min-w-0">
                {endpoint.enabled
                  ? "Receiving events"
                  : "Off: every event is refused"}
              </span>
            </span>
            {canManage && (
              <Button
                variant="outline"
                size="sm"
                disabled={toggle.isPending}
                onClick={() => turn(!endpoint.enabled)}
              >
                {toggle.isPending && <Spinner data-icon="inline-start" />}
                {endpoint.enabled ? "Turn off" : "Turn on"}
              </Button>
            )}
          </Fact>
          <Fact label="URL" align="start">
            <span className="flex min-w-0 flex-1 flex-col gap-1.5">
              <code className="rounded-(--radius-control) border bg-muted px-2.5 py-1.5 font-mono text-2xs break-all text-foreground">
                {endpoint.url}
                <span className="text-muted-foreground">
                  /&lt;event type&gt;
                </span>
              </code>
              <span className="text-2xs text-muted-foreground">
                Followed by the event type's key, e.g. /
                {exampleKey ?? "incident.opened"}
              </span>
            </span>
            <CopyButton value={endpoint.url} label="Copy" />
          </Fact>
          <Fact label="Token">
            <code className="min-w-0 flex-1 truncate font-mono text-2xs text-foreground">
              fevt_••••••••{endpoint.token_hint}
            </code>
            {canManage && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => setConfirming(true)}
              >
                <Icon icon="refresh" data-icon="inline-start" />
                Replace
              </Button>
            )}
          </Fact>
        </dl>
      )}

      <Dialog open={confirming} onOpenChange={setConfirming}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Replace the token?</DialogTitle>
            <DialogDescription>
              The current token stops working at once. Every system that sends{" "}
              {organizationName} events needs the new one.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose render={<Button variant="outline" />}>
              Cancel
            </DialogClose>
            <Button
              variant="destructive"
              disabled={rotate.isPending}
              onClick={() =>
                rotate.mutate(undefined, {
                  onSuccess: (answer) => {
                    setConfirming(false)
                    setRevealed(answer)
                  },
                })
              }
            >
              {rotate.isPending && <Spinner data-icon="inline-start" />}
              Replace token
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <TokenDialog
        endpoint={revealed}
        exampleKey={exampleKey}
        onClose={() => setRevealed(undefined)}
      />
    </section>
  )
}

/** A hairline-ruled row: its label, then its value and any action. */
function Fact({
  label,
  align = "center",
  children,
}: {
  label: string
  align?: "center" | "start"
  children: React.ReactNode
}) {
  return (
    <div className="contents">
      <dt
        className={cn(
          "border-b py-3 text-muted-foreground",
          align === "start" && "pt-[1.125rem]"
        )}
      >
        {label}
      </dt>
      <dd
        className={cn(
          "flex min-w-0 gap-3 border-b py-3",
          align === "center" ? "items-center" : "items-start"
        )}
      >
        {children}
      </dd>
    </div>
  )
}

/** A new token, the one time it's shown, with how to send an event. */
function TokenDialog({
  endpoint,
  exampleKey = "incident.opened",
  onClose,
}: {
  endpoint: EventEndpoint | undefined
  exampleKey?: string
  onClose: () => void
}) {
  // Kept while the dialog closes.
  const [shown, setShown] = React.useState(endpoint)
  if (endpoint && endpoint !== shown) setShown(endpoint)
  const token = shown?.token ?? ""
  const command = shown
    ? curlExample(shown.url, exampleKey, { id: "INC-1042" }, token)
    : ""
  return (
    <Dialog
      open={endpoint !== undefined}
      onOpenChange={(open) => !open && onClose()}
    >
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Copy the endpoint token</DialogTitle>
          <DialogDescription>
            It's shown only now. Store it where the sending systems keep
            secrets; if it's lost, replace it.
          </DialogDescription>
        </DialogHeader>
        <div className="flex min-w-0 items-center gap-2">
          <code className="min-w-0 flex-1 truncate rounded-(--radius-control) border bg-muted px-2.5 py-2 font-mono text-xs select-all">
            {token}
          </code>
          <CopyButton value={token} label="Copy token" />
        </div>
        {shown && (
          <div className="flex min-w-0 flex-col gap-1.5">
            <p className="text-xs text-muted-foreground">
              Send an event, with an Idempotency-Key so retries aren't kept
              twice:
            </p>
            <pre
              tabIndex={0}
              className="overflow-auto rounded-(--radius-control) border bg-muted p-3 font-mono text-2xs/[1.6] whitespace-pre text-foreground"
            >
              {command}
            </pre>
          </div>
        )}
        <DialogFooter>
          {shown && <CopyButton value={command} label="Copy command" />}
          <DialogClose render={<Button />}>I've stored it</DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
