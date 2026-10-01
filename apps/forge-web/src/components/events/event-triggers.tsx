import * as React from "react"
import { Link, useNavigate } from "@tanstack/react-router"
import { cn } from "cn"

import {
  ADMIN_TABLE_FEATURES,
  PIN_ACTIONS,
} from "@/components/admin/table-config"
import { RowMenu } from "@/components/admin/table-parts"
import {
  createColumnHelper,
  DataTable,
  type DataTableFeatureConfig,
  type DataTableOptions,
  type InitialTableState,
} from "@/components/forge/data-table"
import { PanelEmpty } from "@/components/forge/empty-state"
import { ErrorCallout } from "@/components/forge/feedback"
import { Icon } from "@/components/forge/icon"
import { Chip } from "@/components/forge/status"
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
import { DropdownMenuItem } from "@/components/ui/dropdown-menu"
import { toast } from "@/components/ui/toast"
import { formatRelative } from "@/lib/format"
import { eventTypes, type EventType } from "@/lib/events"
import type { WorkflowDocument } from "@/lib/workflows/document"
import {
  TRIGGERS,
  WORKFLOWS_ICON,
  type TriggerKind,
} from "@/lib/workflows/model"
import { useSaveWorkflows, useOrganizationWorkflows } from "@/lib/workflows/api"
import {
  entryOf,
  eventKeysOf,
  linkEvent,
  unlinkEvent,
} from "@/lib/workflows/triggers"
import { parseTimestamp } from "@/lib/timestamps"

/** A row of the table: one of the organization's workflows. */
type TriggerRow = {
  id: string
  name: string
  description: string
  /** This event type starts it. */
  linked: boolean
  /** How it starts now; none without an entry point. */
  start: TriggerKind | "none"
  /** The other event types that start it, by name (or key, if gone). */
  others: string[]
  steps: number
  updated_at: string
  doc: WorkflowDocument
}

// Linked workflows first, then those a link would change the least.
const ORDER: Record<TriggerRow["start"], number> = {
  event: 1,
  manual: 2,
  schedule: 3,
  none: 4,
}
const orderOf = (row: TriggerRow) => (row.linked ? 0 : ORDER[row.start])

function toRow(
  doc: WorkflowDocument,
  key: string,
  names: Map<string, string>
): TriggerRow {
  const keys = eventKeysOf(doc)
  return {
    id: doc.id,
    name: doc.name,
    description: doc.description,
    linked: keys.includes(key),
    start: entryOf(doc)?.config.trigger ?? "none",
    others: keys.filter((k) => k !== key).map((k) => names.get(k) ?? k),
    steps: doc.nodes.length,
    updated_at: doc.updated_at,
    doc,
  }
}

/** A workflow starts on a schedule or on events: linking stops its schedule. */
const onSchedule = (row: TriggerRow) => row.start === "schedule"

// A table to pick from: its toolbar keeps room for Link and Unlink, so
// selecting a row doesn't wrap it and move the rows under the pointer.
const FEATURES: Partial<DataTableFeatureConfig> = {
  ...ADMIN_TABLE_FEATURES,
  rowSelection: true,
  pagination: false,
  faceting: false,
  columnVisibility: false,
  export: false,
}

const INITIAL_STATE: InitialTableState = { columnPinning: PIN_ACTIONS }

// A workflow without an entry point has nothing to link.
const TABLE_OPTIONS: DataTableOptions<TriggerRow> = {
  enableRowSelection: (row) => row.original.start !== "none",
}

// Row actions reach the cells through context, so the columns stay put.
type RowActions = {
  eventKey: string
  open: (row: TriggerRow) => void
  /** `done` runs once they're saved: the selection clears. */
  link: (rows: TriggerRow[], done?: () => void) => void
  unlink: (rows: TriggerRow[], done?: () => void) => void
}
const ActionsContext = React.createContext<RowActions | undefined>(undefined)

const helper = createColumnHelper<TriggerRow>()
const COLUMNS = helper.columns([
  helper.accessor("name", {
    header: "Workflow",
    size: 300,
    enableHiding: false,
    meta: { label: "Workflow" },
    cell: ({ row: { original: row } }) => (
      <span className="flex min-w-0 flex-col">
        <span className="truncate text-[0.8125rem] font-medium text-foreground">
          {row.name}
        </span>
        <span className="truncate text-2xs text-muted-foreground">
          {row.description || "No description"}
        </span>
      </span>
    ),
  }),
  helper.accessor(orderOf, {
    id: "start",
    header: "Starts",
    size: 260,
    meta: {
      label: "Starts",
      // Sorts by its order, but reads as text.
      align: "left",
      cellClassName: "text-muted-foreground",
    },
    cell: ({ row: { original: row } }) => <StartCell row={row} />,
  }),
  helper.accessor("steps", {
    header: "Steps",
    size: 96,
    meta: { label: "Steps", align: "right" },
    cell: ({ getValue }) => (
      <span className="tabular-nums">
        {getValue()} {getValue() === 1 ? "step" : "steps"}
      </span>
    ),
  }),
  helper.accessor("updated_at", {
    header: "Changed",
    size: 120,
    meta: { label: "Changed", cellClassName: "text-muted-foreground" },
    cell: ({ getValue }) => (
      <span title={parseTimestamp(getValue()).toLocaleString()}>
        {formatRelative(getValue())}
      </span>
    ),
  }),
  helper.display({
    id: "actions",
    header: () => <span className="sr-only">Actions</span>,
    size: 64,
    enableSorting: false,
    enableHiding: false,
    enableResizing: false,
    meta: { label: "Actions", align: "right" },
    cell: ({ row }) => <TriggerMenu row={row.original} />,
  }),
])

/** Whether this event type starts it, and what else does. */
function StartCell({ row }: { row: TriggerRow }) {
  if (row.start === "none") {
    return <span className="text-subtle">No entry point</span>
  }
  const others = row.others.join(", ")
  if (row.linked) {
    return (
      <span className="flex min-w-0 items-center gap-2">
        <Chip tone="success" icon="link" className="shrink-0">
          Linked
        </Chip>
        {others && (
          <span className="truncate" title={`Also starts on ${others}`}>
            + {others}
          </span>
        )}
      </span>
    )
  }
  const trigger = TRIGGERS[row.start]
  return (
    <span className="flex min-w-0 items-center gap-1.5">
      <Icon icon={trigger.icon} size={14} className="shrink-0" />
      {row.start !== "event" ? (
        trigger.label
      ) : others ? (
        <span className="truncate" title={`Starts on ${others}`}>
          On {others}
        </span>
      ) : (
        <span className="text-subtle">No event types picked</span>
      )}
    </span>
  )
}

function TriggerMenu({ row }: { row: TriggerRow }) {
  const actions = React.useContext(ActionsContext)
  if (!actions) return null
  return (
    <RowMenu
      label={row.name}
      onEdit={() => actions.open(row)}
      editLabel="Open in builder"
      editIcon={WORKFLOWS_ICON}
    >
      {row.linked ? (
        <DropdownMenuItem onClick={() => actions.unlink([row])}>
          <Icon icon="close" />
          Unlink
        </DropdownMenuItem>
      ) : (
        <DropdownMenuItem
          disabled={row.start === "none"}
          onClick={() => actions.link([row])}
        >
          <Icon icon="link" />
          Link to {actions.eventKey}
        </DropdownMenuItem>
      )}
    </RowMenu>
  )
}

const count = (n: number) => (n === 1 ? "1 workflow" : `${n} workflows`)

/**
 * An event type's Triggers: the organization's workflows, and which of them it
 * starts. An event type starts any number of workflows, and a workflow
 * starts on any number of event types: select workflows and Link them, and
 * this event type joins the event types their entry points start on;
 * Unlink takes it out again, leaving the others. A workflow on a schedule
 * stops its schedule when linked, so it's asked about first.
 */
export function EventTriggers({
  organizationId,
  eventType,
  className,
}: {
  organizationId: string
  eventType: EventType
  className?: string
}) {
  const id = React.useId()
  const navigate = useNavigate()
  const { workflows, records, error, refetch } = useOrganizationWorkflows(organizationId)
  const saveWorkflows = useSaveWorkflows(organizationId)
  const types = eventTypes.scope({ organizationId }).useList()
  // Waiting for the question about workflows on a schedule.
  const [confirming, setConfirming] = React.useState<{
    rows: TriggerRow[]
    done?: () => void
  }>()

  const key = eventType.key
  const rows = React.useMemo(() => {
    const names = new Map((types.data ?? []).map((t) => [t.key, t.name]))
    return workflows
      .map((doc) => toRow(doc, key, names))
      .sort((a, b) => orderOf(a) - orderOf(b))
  }, [workflows, key, types.data])
  const linked = rows.filter((row) => row.linked).length

  const change = React.useCallback(
    async (targets: TriggerRow[], link: boolean, done?: () => void) => {
      const changed = targets.map((row) =>
        link ? linkEvent(row.doc, key) : unlinkEvent(row.doc, key)
      )
      const byId = new Map(records.map((record) => [record.id, record]))
      const result = await saveWorkflows(
        changed.flatMap((document) => {
          const record = byId.get(document.id)
          return record ? [{ record, document }] : []
        })
      )
      if (!result.ok) {
        toast.add({
          title: link
            ? "Couldn't link the workflows"
            : "Couldn't unlink the workflows",
          description: result.error,
          type: "error",
        })
        return
      }
      done?.()
      const who = targets.length === 1 ? targets[0].name : count(targets.length)
      // Unlinked from their last event type, they start by hand.
      const byHand = changed.filter(
        (doc) => eventKeysOf(doc).length === 0
      ).length
      toast.add({
        title: link
          ? `${who} linked to ${eventType.name}.`
          : `${who} unlinked from ${eventType.name}.`,
        description: link
          ? eventType.status === "active"
            ? undefined
            : `${eventType.name} is ${eventType.status === "draft" ? "a draft" : "paused"}: it accepts no events until it's turned on.`
          : byHand === 0
            ? undefined
            : byHand === targets.length
              ? `${targets.length === 1 ? "It starts" : "They start"} by hand now.`
              : `${count(byHand)} with no other event type start by hand now.`,
        type: "success",
      })
    },
    [eventType.name, eventType.status, key, records, saveWorkflows]
  )

  const actions = React.useMemo<RowActions>(
    () => ({
      eventKey: key,
      open: (row) =>
        void navigate({
          to: "/organizations/$organizationId/workflows/$workflowId",
          params: { organizationId, workflowId: row.id },
        }),
      link: (targets, done) => {
        const toLink = targets.filter(
          (row) => !row.linked && row.start !== "none"
        )
        if (toLink.some(onSchedule)) setConfirming({ rows: toLink, done })
        else if (toLink.length) change(toLink, true, done)
      },
      unlink: (targets, done) =>
        change(
          targets.filter((row) => row.linked),
          false,
          done
        ),
    }),
    [change, key, navigate, organizationId]
  )

  const scheduled = confirming?.rows.filter(onSchedule) ?? []

  return (
    <section
      aria-labelledby={`${id}-title`}
      className={cn("flex flex-col gap-5", className)}
    >
      <div className="flex flex-col gap-1">
        <h2 id={`${id}-title`} className="text-sm font-medium">
          Workflows it starts
        </h2>
        <p className="max-w-[78ch] text-[0.8125rem] text-muted-foreground">
          Select the workflows a valid{" "}
          <code className="font-mono text-xs text-foreground">{key}</code> event
          should start and link them; each run gets the event&apos;s payload as
          its input. It can start any number of workflows, and a workflow can
          start on several event types: linking adds this one to the
          workflow&apos;s entry point and keeps the others.
        </p>
      </div>

      <p className="flex max-w-[78ch] items-start gap-2 rounded-(--radius-card) border px-3 py-2 text-[0.8125rem] text-muted-foreground">
        <Icon icon="info" size={15} className="mt-0.5 shrink-0" />
        A link is kept in the workflow&apos;s start step, saved with the
        organization&apos;s workflows. Forge doesn&apos;t start runs from events
        yet; linked workflows will start on these events once it does.
      </p>

      {error ? (
        <ErrorCallout
          title="Couldn't load the organization's workflows"
          action={
            <Button variant="outline" size="sm" onClick={() => void refetch()}>
              Retry
            </Button>
          }
        >
          {error.message}
        </ErrorCallout>
      ) : rows.length === 0 ? (
        <PanelEmpty
          illustration="waiting"
          className="rounded-(--radius-card) border"
        >
          The organization has no workflows to link yet.{" "}
          <Link
            to="/organizations/$organizationId"
            params={{ organizationId }}
            search={{ view: "workflows" }}
            className="font-medium text-foreground underline-offset-2 hover:underline"
          >
            Build one on Workflows
          </Link>
          .
        </PanelEmpty>
      ) : (
        <ActionsContext.Provider value={actions}>
          <DataTable
            description={
              linked
                ? `${count(linked)} linked · ${rows.length} in all`
                : `None linked yet · ${count(rows.length)} in all`
            }
            columns={COLUMNS}
            data={rows}
            getRowId={(row) => row.id}
            features={FEATURES}
            initialState={INITIAL_STATE}
            tableOptions={TABLE_OPTIONS}
            stateKey="event-type-triggers"
            labels={{ rows: "workflows", rowSingular: "workflow" }}
            onRowClick={(row) => {
              if (row.getCanSelect()) row.toggleSelected()
            }}
            selectedRowsActions={({ selectedRows, table }) => {
              const done = () => table.resetRowSelection()
              const picked = selectedRows.map((row) => row.original)
              const toLink = picked.filter((row) => !row.linked).length
              const toUnlink = picked.length - toLink
              return (
                <>
                  {toUnlink > 0 && (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => actions.unlink(picked, done)}
                    >
                      Unlink{toLink > 0 && ` ${toUnlink}`}
                    </Button>
                  )}
                  {toLink > 0 && (
                    <Button
                      size="sm"
                      onClick={() => actions.link(picked, done)}
                    >
                      <Icon icon="link" data-icon="inline-start" />
                      Link{toUnlink > 0 && ` ${toLink}`}
                    </Button>
                  )}
                </>
              )
            }}
          />
        </ActionsContext.Provider>
      )}

      <Dialog
        open={Boolean(confirming)}
        onOpenChange={(open) => !open && setConfirming(undefined)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {scheduled.length === 1
                ? `${scheduled[0].name} runs on a schedule`
                : `${scheduled.length} of them run on a schedule`}
            </DialogTitle>
            <DialogDescription>
              A workflow starts on a schedule or on events, not both. Linking{" "}
              {scheduled.length === 1 ? "it" : "them"} to {eventType.name} turns{" "}
              {scheduled.length === 1 ? "its" : "their"} schedule off; the cron
              settings stay in the builder.
            </DialogDescription>
          </DialogHeader>
          <ul className="flex flex-col gap-1.5 text-[0.8125rem]">
            {scheduled.map((row) => (
              <li key={row.id} className="truncate font-medium">
                {row.name}
              </li>
            ))}
          </ul>
          <DialogFooter>
            <DialogClose render={<Button type="button" variant="outline" />}>
              Cancel
            </DialogClose>
            <Button
              onClick={() => {
                if (confirming) change(confirming.rows, true, confirming.done)
                setConfirming(undefined)
              }}
            >
              {confirming && confirming.rows.length > scheduled.length
                ? `Link all ${confirming.rows.length}`
                : "Link anyway"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  )
}
