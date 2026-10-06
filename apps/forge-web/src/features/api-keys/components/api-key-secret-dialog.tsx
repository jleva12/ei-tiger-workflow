import { Copyable } from "@/components/forge/copyable"
import { Icon } from "@/components/forge/icon"
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
import { runtimeAddress } from "@/lib/runtime"
import type { ApiKeyCreated } from "../lib/api"

/**
 * A key just made, shown this once: the key, how to send it, and a call to
 * try it with. Closing it is the last time anyone sees the key.
 */
export function ApiKeySecretDialog({
  apiKey,
  onClose,
}: {
  apiKey: ApiKeyCreated | undefined
  onClose: () => void
}) {
  const curl = apiKey
    ? [
        `curl ${runtimeAddress("/run_sse")} \\`,
        `  -H 'Authorization: Bearer ${apiKey.secret}' \\`,
        `  -H 'Content-Type: application/json' \\`,
        `  -d '{"appName": "ca_…", "userId": "customer-1", "sessionId": "…", "newMessage": {"role": "user", "parts": [{"text": "Hello"}]}}'`,
      ].join("\n")
    : ""
  return (
    <Dialog
      open={apiKey !== undefined}
      onOpenChange={(open) => !open && onClose()}
    >
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Copy {apiKey?.name ?? "the key"} now</DialogTitle>
          <DialogDescription>
            This is the only time it&apos;s shown: Forge keeps only a digest of
            it. Put it where the app keeps its secrets.
          </DialogDescription>
        </DialogHeader>
        {apiKey && (
          <div className="flex min-w-0 flex-col gap-3">
            <Copyable label="API key" value={apiKey.secret} />
            <Copyable
              label="Header"
              value={`Authorization: Bearer ${apiKey.secret}`}
            />
            <Copyable
              label="Try it: a turn with one of the agents"
              value={curl}
              multiline
            />
            <p className="flex items-start gap-2 text-xs/[1.6] text-muted-foreground">
              <Icon icon="info" size={14} className="mt-0.5 shrink-0" />
              <span>
                It can do what {apiKey.role_name ?? "its role"} allows in this
                organization, and nowhere else. Lost it? Delete it and make
                another.
              </span>
            </p>
          </div>
        )}
        <DialogFooter>
          <DialogClose render={<Button type="button" />}>
            I&apos;ve copied it
          </DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
