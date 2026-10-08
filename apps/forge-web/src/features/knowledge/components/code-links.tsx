import * as React from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"

import { ErrorCallout } from "@/components/forge/feedback"
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
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupInput,
} from "@/components/ui/input-group"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { toast } from "@/components/ui/toast"
import { DeleteDialog } from "@/features/admin/components/delete-dialog"
import { useOpenCount } from "@/features/admin/components/dialog-state"
import { KindBadge } from "@/features/code-graph/components/node-mark"
import { codeGraphApi, codeGraphKeys } from "@/features/code-graph/lib/api"
import type { NodeVersion } from "@/features/code-graph/lib/graph"
import type { CodeRepository } from "@/features/code-repositories/lib/api"
import type { KnowledgeScope } from "../lib/api"
import {
  CONNECTION_KINDS,
  codeLinks,
  connections,
  type CodeEnd,
  type CodeLink,
  type Connection,
} from "../lib/system"

/** The connection's two applications, as the map places them. */
type Ends = { source: CodeRepository; target: CodeRepository }

/**
 * Where a connection happens in the code: its code links, each a
 * declaration in the source application (the method that calls the API or
 * publishes the event) and one in the target (the handler or listener).
 * They're written into the code graph, so its queries, and agents, follow
 * the code from one application into the other. Those who manage knowledge
 * bases add and remove them; both applications must be in the code graph
 * first.
 */
export function CodeLinksSection({
  scope,
  connection,
  ends,
  canManage,
  onOpenNode,
}: {
  scope: KnowledgeScope
  connection: Connection
  ends: Ends
  canManage: boolean
  /** Opens an application's code graph at a node. */
  onOpenNode: (repositoryId: string, nodeId: string) => void
}) {
  const queryClient = useQueryClient()
  const scoped = codeLinks.scope({ ...scope, connectionId: connection.id })
  const list = scoped.useList()
  // Removing changes the connection's count on the map too.
  const remove = scoped.useDelete({
    meta: { silent: true },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: connections.keys.all }),
  })
  const [adding, setAdding] = React.useState(false)
  const [removing, setRemoving] = React.useState<CodeLink>()
  const ingested = !!ends.source.last_success && !!ends.target.last_success

  return (
    <section className="flex flex-col gap-2 border-t pt-3">
      <div className="flex items-center justify-between gap-2">
        <h5 className="text-2xs font-medium text-muted-foreground">
          In the code
        </h5>
        {canManage && (
          <Button
            variant="ghost"
            size="xs"
            disabled={!ingested}
            onClick={() => setAdding(true)}
          >
            <Icon icon="plus" data-icon="inline-start" />
            Link the code
          </Button>
        )}
      </div>
      {list.isPending ? (
        <Skeleton className="h-10" />
      ) : list.error ? (
        <p className="text-2xs text-destructive" role="alert">
          {list.error.message}
        </p>
      ) : list.data.length === 0 ? (
        <p className="text-subtle">
          {!ingested
            ? `Ingest ${ends.source.last_success ? ends.target.name : ends.source.name} into the code graph to say where in its code this happens.`
            : canManage
              ? `Say which ${ends.source.name} code reaches which ${ends.target.name} code, so the code graph can follow it across.`
              : "Nobody has said where in the code this happens yet."}
        </p>
      ) : (
        <ul className="-mx-1 flex flex-col gap-0.5" aria-label="Code links">
          {list.data.map((code) => (
            <li
              key={code.id}
              className="flex items-start gap-1 rounded-(--radius-item) px-1.5 py-1 hover:bg-muted"
            >
              <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                <EndLine
                  repo={ends.source}
                  end={code.source}
                  onOpen={onOpenNode}
                />
                <span className="text-2xs text-subtle">
                  ↓ {CONNECTION_KINDS[connection.kind].label}
                  {code.label && ` · ${code.label}`}
                </span>
                <EndLine
                  repo={ends.target}
                  end={code.target}
                  onOpen={onOpenNode}
                />
              </div>
              {canManage && (
                <Button
                  variant="ghost"
                  size="icon-xs"
                  aria-label={`Remove the code link from ${code.source.name} to ${code.target.name}`}
                  onClick={() => setRemoving(code)}
                >
                  <Icon icon="close" />
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {canManage && ingested && (
        <CodeLinkDialog
          open={adding}
          onOpenChange={setAdding}
          scope={scope}
          connection={connection}
          ends={ends}
        />
      )}
      <DeleteDialog
        open={removing !== undefined}
        onClose={() => setRemoving(undefined)}
        title={`Remove the code link from ${removing?.source.name ?? "it"} to ${removing?.target.name ?? "the other"}?`}
        description="The code graph stops following it from one application into the other. Nothing changes in either repository."
        confirmLabel="Remove code link"
        onConfirm={() => remove.mutateAsync(removing!.id)}
      />
    </section>
  )
}

/** One end of a code link: the declaration, opening in its application's graph. */
function EndLine({
  repo,
  end,
  onOpen,
}: {
  repo: CodeRepository
  end: CodeEnd
  onOpen: (repositoryId: string, nodeId: string) => void
}) {
  return (
    <button
      type="button"
      onClick={() => onOpen(repo.id, end.node_id)}
      title={`${end.qualified_name || end.name} in ${repo.owner}/${repo.name}${end.path ? ` (${end.path})` : ""}`}
      className="flex min-w-0 items-center gap-1.5 text-left text-xs hover:underline"
    >
      <KindBadge kind={end.kind} size={14} />
      <span className="truncate text-foreground">
        {end.qualified_name || end.name}
      </span>
    </button>
  )
}

function CodeLinkDialog({
  open,
  onOpenChange,
  ...props
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  scope: KnowledgeScope
  connection: Connection
  ends: Ends
}) {
  const count = useOpenCount(open)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <CodeLinkForm
          key={count}
          onDone={() => onOpenChange(false)}
          {...props}
        />
      </DialogContent>
    </Dialog>
  )
}

function CodeLinkForm({
  scope,
  connection,
  ends,
  onDone,
}: {
  scope: KnowledgeScope
  connection: Connection
  ends: Ends
  onDone: () => void
}) {
  const id = React.useId()
  const queryClient = useQueryClient()
  // The form shows why it failed.
  const create = codeLinks
    .scope({ ...scope, connectionId: connection.id })
    .useCreate({
      meta: { silent: true },
      onSuccess: () =>
        queryClient.invalidateQueries({ queryKey: connections.keys.all }),
    })
  const [source, setSource] = React.useState<NodeVersion>()
  const [target, setTarget] = React.useState<NodeVersion>()
  const [label, setLabel] = React.useState("")
  const [submitted, setSubmitted] = React.useState(false)
  const kind = CONNECTION_KINDS[connection.kind]

  function submit(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitted(true)
    if (!source || !target) return
    create.mutate(
      {
        source_node_id: source.fact.node.id,
        target_node_id: target.fact.node.id,
        label: label.trim(),
      },
      {
        onSuccess: (code) => {
          toast.add({
            title: `Linked ${code.source.name} to ${code.target.name}`,
            description:
              "The code graph now follows it from one application into the other.",
            type: "success",
          })
          onDone()
        },
      }
    )
  }

  return (
    <form onSubmit={submit} noValidate className="grid gap-6">
      <DialogHeader>
        <DialogTitle>Link the code</DialogTitle>
        <DialogDescription>
          {`${ends.source.name} ${kind.label.toLowerCase()} ${ends.target.name}. Pick where in the code: the ${ends.source.name} declaration that does it and the ${ends.target.name} one it reaches, such as the client method and the handler serving it, or the publisher and the listener.`}
        </DialogDescription>
      </DialogHeader>
      {create.error && (
        <ErrorCallout title="Couldn't link them">
          {create.error.message}
        </ErrorCallout>
      )}
      <FieldGroup>
        <SymbolPicker
          id={`${id}-source`}
          organizationId={scope.organizationId}
          repo={ends.source}
          label={`In ${ends.source.name}`}
          hint="The method that calls the API or sends the event."
          picked={source}
          onPick={setSource}
          invalid={submitted && !source}
        />
        <SymbolPicker
          id={`${id}-target`}
          organizationId={scope.organizationId}
          repo={ends.target}
          label={`In ${ends.target.name}`}
          hint="The handler that serves it, or the listener that consumes it."
          picked={target}
          onPick={setTarget}
          invalid={submitted && !target}
        />
        <Field>
          <FieldLabel htmlFor={`${id}-label`}>Label</FieldLabel>
          <Input
            id={`${id}-label`}
            value={label}
            maxLength={500}
            placeholder="Optional, e.g. POST /v1/orders, or orders.created"
            onChange={(event) => setLabel(event.target.value)}
          />
        </Field>
      </FieldGroup>
      <DialogFooter>
        <DialogClose render={<Button variant="outline" />}>Cancel</DialogClose>
        <Button type="submit" disabled={create.isPending}>
          {create.isPending && <Spinner data-icon="inline-start" />}
          Link
        </Button>
      </DialogFooter>
    </form>
  )
}

/**
 * Picks a declaration of an application's live code graph by its exact
 * name. Enter searches rather than submitting the dialog's form.
 */
function SymbolPicker({
  id,
  organizationId,
  repo,
  label,
  hint,
  picked,
  onPick,
  invalid,
}: {
  id: string
  organizationId: string
  repo: CodeRepository
  label: string
  hint: string
  picked: NodeVersion | undefined
  onPick: (node: NodeVersion | undefined) => void
  invalid: boolean
}) {
  const graph = React.useMemo(
    () => codeGraphApi(organizationId, repo.id),
    [organizationId, repo.id]
  )
  const [query, setQuery] = React.useState("")
  const [searched, setSearched] = React.useState("")
  const results = useQuery({
    queryKey: codeGraphKeys.symbols(organizationId, repo.id, 0, searched),
    queryFn: ({ signal }) => graph.symbols(searched, 0, signal),
    enabled: searched !== "",
    // Shown here, not as a toast.
    meta: { silent: true },
  })
  const search = () => setSearched(query.trim())

  return (
    <Field data-invalid={invalid}>
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      {picked ? (
        <div className="flex min-w-0 items-center gap-2 rounded-(--radius-control) border px-2.5 py-1.5">
          <KindBadge kind={picked.fact.node.kind} size={16} />
          <span className="flex min-w-0 flex-1 flex-col">
            <span className="truncate text-xs text-foreground">
              {picked.fact.node.qualified_name || picked.fact.node.name}
            </span>
            <span className="truncate text-2xs text-muted-foreground">
              {picked.fact.node.properties?.file_path?.string ??
                picked.fact.node.kind}
            </span>
          </span>
          <Button
            type="button"
            variant="ghost"
            size="xs"
            onClick={() => onPick(undefined)}
          >
            Change
          </Button>
        </div>
      ) : (
        <>
          <InputGroup>
            <InputGroupInput
              id={id}
              placeholder="Exact name, e.g. createOrder"
              value={query}
              aria-invalid={invalid}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault()
                  search()
                }
              }}
            />
            <InputGroupAddon align="inline-end">
              <InputGroupButton
                type="button"
                size="icon-xs"
                aria-label={`Find in ${repo.name}`}
                disabled={!query.trim()}
                onClick={search}
              >
                {results.isFetching ? <Spinner /> : <Icon icon="search" />}
              </InputGroupButton>
            </InputGroupAddon>
          </InputGroup>
          {results.error ? (
            <p className="text-2xs text-destructive" role="alert">
              {results.error.message}
            </p>
          ) : results.data ? (
            results.data.nodes.length ? (
              <ul
                className="flex max-h-36 flex-col overflow-y-auto rounded-(--radius-control) border p-1"
                aria-label={`Declarations named ${searched}`}
              >
                {results.data.nodes.map((v) => (
                  <li key={v.fact.node.id}>
                    <button
                      type="button"
                      onClick={() => onPick(v)}
                      className="flex w-full min-w-0 items-center gap-2 rounded-(--radius-item) px-1.5 py-1 text-left text-xs hover:bg-muted"
                    >
                      <KindBadge kind={v.fact.node.kind} size={16} />
                      <span className="flex min-w-0 flex-col">
                        <span className="truncate text-foreground">
                          {v.fact.node.qualified_name || v.fact.node.name}
                        </span>
                        <span className="truncate text-2xs text-subtle">
                          {v.fact.node.properties?.file_path?.string ??
                            v.fact.node.kind}
                        </span>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-2xs text-subtle">
                {`Nothing in ${repo.name} is named exactly ${searched}.`}
              </p>
            )
          ) : null}
          <FieldDescription>{hint}</FieldDescription>
        </>
      )}
      {invalid && <FieldError>Pick a declaration.</FieldError>}
    </Field>
  )
}
