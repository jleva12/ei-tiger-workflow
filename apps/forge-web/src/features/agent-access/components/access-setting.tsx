import * as React from "react"
import { Link } from "@tanstack/react-router"

import { Icon } from "@/components/forge/icon"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Field,
  FieldContent,
  FieldDescription,
  FieldLabel,
} from "@/components/ui/field"
import { Skeleton } from "@/components/ui/skeleton"
import { agentPermissions } from "../lib/api"

type AccessSettingProps = {
  organizationId: string
  /** What it limits, for sentences: "use it" (an agent), "run it" (a workflow). */
  verb: "use" | "run"
  /** The agent permissions picked: any one of them lets someone in. */
  chosen: number[]
  canManage: boolean
  saving: boolean
  /** Saves the new pick at once. */
  onChange: (permissionIds: number[]) => void
  /** Said too while it's limited, e.g. that its chat page doesn't answer. */
  limitedNote?: string
}

/**
 * Who may use a chat agent or run a workflow: some of its organization's
 * agent permissions, any one of them (set up under Settings → Agent
 * access), or, with none, anyone who may call the organization's agents.
 * Those who build its agents always may. It holds for every version at
 * once; each change saves at once.
 */
export function AccessSetting({
  organizationId,
  verb,
  chosen,
  canManage,
  saving,
  onChange,
  limitedNote,
}: AccessSettingProps) {
  const id = React.useId()
  // Reading them takes members:read: without it, the count is all there is.
  const list = agentPermissions
    .scope({ organizationId })
    .useList(undefined, { retry: false })
  const known = list.data ?? []
  // Chosen once, deleted since: still listed, so they can be let go.
  const deleted = list.data
    ? chosen.filter((chosenId) => !known.some((p) => p.id === chosenId))
    : []
  const toggle = (permissionId: number, on: boolean) =>
    onChange(
      on
        ? [...chosen, permissionId]
        : chosen.filter((other) => other !== permissionId)
    )

  return (
    <section className="flex flex-col gap-2.5 border-b px-4 py-3.5">
      <h3 className="text-xs font-medium text-foreground">Who may {verb} it</h3>
      <p className="text-xs/[1.6] text-muted-foreground">
        {chosen.length === 0
          ? `Anyone who may call the organization's agents. Pick agent permissions to let only those holding one ${verb} it, every version of it.`
          : `Only those holding ${chosen.length === 1 ? "this agent permission" : "one of these agent permissions"} (through a linked company group, or an API key linked to it), and those who build the organization's agents.${limitedNote ? ` ${limitedNote}` : ""}`}
      </p>
      {list.isPending ? (
        <div className="flex flex-col gap-2" aria-busy="true">
          <Skeleton className="h-5" />
          <Skeleton className="h-5" />
        </div>
      ) : list.error ? (
        <p className="text-xs/[1.6] text-muted-foreground">
          {list.error.status === 403
            ? `You can't see the organization's agent permissions${chosen.length ? `; ${chosen.length === 1 ? "one is" : `${chosen.length} are`} picked` : ""}.`
            : `Couldn't load the agent permissions: ${list.error.message}`}
        </p>
      ) : known.length === 0 && deleted.length === 0 ? (
        <p className="text-xs/[1.6] text-muted-foreground">
          The organization has no agent permissions yet.
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          {known.map((permission) => {
            const checkboxId = `${id}-${permission.id}`
            return (
              <Field key={permission.id} orientation="horizontal">
                <Checkbox
                  id={checkboxId}
                  disabled={!canManage || saving}
                  checked={chosen.includes(permission.id)}
                  onCheckedChange={(on) => toggle(permission.id, on)}
                />
                <FieldContent>
                  <FieldLabel
                    htmlFor={checkboxId}
                    className="font-mono text-xs font-normal"
                  >
                    {permission.key}
                  </FieldLabel>
                  {permission.description && (
                    <FieldDescription className="text-xs">
                      {permission.description}
                    </FieldDescription>
                  )}
                </FieldContent>
              </Field>
            )
          })}
          {deleted.map((deletedId) => {
            const checkboxId = `${id}-${deletedId}`
            return (
              <Field key={deletedId} orientation="horizontal">
                <Checkbox
                  id={checkboxId}
                  disabled={!canManage || saving}
                  checked
                  onCheckedChange={(on) => toggle(deletedId, on)}
                />
                <FieldContent>
                  <FieldLabel
                    htmlFor={checkboxId}
                    className="text-xs font-normal text-muted-foreground"
                  >
                    A deleted agent permission
                  </FieldLabel>
                  <FieldDescription className="text-xs">
                    Nobody holds it: only those who build agents may {verb} it.
                  </FieldDescription>
                </FieldContent>
              </Field>
            )
          })}
        </div>
      )}
      <Button
        variant="outline"
        size="xs"
        className="self-start"
        nativeButton={false}
        render={
          <Link
            to="/organizations/$organizationId"
            params={{ organizationId }}
            search={{ view: "agent-access" }}
          />
        }
      >
        <Icon icon="settings" data-icon="inline-start" />
        Agent access
      </Button>
    </section>
  )
}
