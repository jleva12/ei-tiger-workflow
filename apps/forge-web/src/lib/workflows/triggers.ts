import type { WorkflowDocument, WorkflowNode } from "./document"

/*
 * What starts a workflow is its entry point's trigger, so linking an event
 * type to a workflow is adding the event type's key to the entry point's
 * `event_types`. An event type starts any number of workflows, and a
 * workflow starts on any number of event types: linking one never unlinks
 * another. The event type page and the builder read and write the same
 * setting; there's no separate list of links to keep in step.
 */

type EntryConfig = WorkflowNode<"entry">["config"]

/** The workflow's entry point; none when it has no entry step. */
export function entryOf(doc: WorkflowDocument): WorkflowNode<"entry"> | undefined {
  const node = doc.nodes.find((n) => n.id === doc.entry)
  return node?.kind === "entry" ? node : undefined
}

/** The keys of the event types that start the workflow. */
export function eventKeysOf(doc: WorkflowDocument): string[] {
  const entry = entryOf(doc)
  return entry?.config.trigger === "event" ? entry.config.event_types : []
}

/** The workflow with its entry point's settings changed; unchanged without one. */
function withEntry(
  doc: WorkflowDocument,
  change: (config: EntryConfig) => EntryConfig
): WorkflowDocument {
  const entry = entryOf(doc)
  if (!entry) return doc
  const config = change(entry.config)
  return {
    ...doc,
    nodes: doc.nodes.map((n) => (n.id === entry.id ? { ...entry, config } : n)),
    updated_at: new Date().toISOString(),
  }
}

/**
 * The workflow started by events of `key` too. One that started by hand or
 * on a schedule starts on events from now on; its schedule settings are
 * kept, for switching back in the builder.
 */
export const linkEvent = (doc: WorkflowDocument, key: string) =>
  withEntry(doc, (config) => ({
    ...config,
    trigger: "event",
    event_types:
      config.trigger === "event"
        ? [...new Set([...config.event_types, key])]
        : [key],
  }))

/**
 * The workflow no longer started by events of `key`. Unlinked from its last
 * event type, it starts by hand.
 */
export const unlinkEvent = (doc: WorkflowDocument, key: string) =>
  withEntry(doc, (config) => {
    const rest = config.event_types.filter((k) => k !== key)
    return {
      ...config,
      trigger: rest.length ? config.trigger : "manual",
      event_types: rest,
    }
  })

/** The workflow following an event type whose key changed. */
export const renameEvent = (doc: WorkflowDocument, from: string, to: string) =>
  withEntry(doc, (config) => ({
    ...config,
    event_types: [
      ...new Set(config.event_types.map((k) => (k === from ? to : k))),
    ],
  }))
