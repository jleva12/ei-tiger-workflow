import type { CodeRepository } from "@/features/code-repositories/lib/api"
import {
  COMPONENT_KINDS,
  type Component,
  type ComponentKind,
  type Connection,
  type Group,
  type MapEnd,
} from "./system"

/*
 * What the system map, its inspector and its dialogs share: the things a
 * connection can join (an application or a component), how a select names
 * one, and what's being added, changed or removed.
 */

// Keys in a column as wide as the longest; values take the rest.
export const FACTS =
  "grid grid-cols-[fit-content(40%)_minmax(0,1fr)] gap-x-4 gap-y-2"

export const plural = (count: number, word: string) =>
  `${count} ${word}${count === 1 ? "" : "s"}`

/** Something on the map a connection can join, with what it is. */
export type Part =
  | {
      type: "application"
      id: string
      name: string
      color: number
      repo: CodeRepository
    }
  | { type: "component"; id: string; name: string; component: Component }

export type Parts = Map<string, Part>

/** The ID a select names an end by. */
export const endValue = (end: MapEnd) => `${end.type}:${end.id}`

export function endFrom(value: string): MapEnd | undefined {
  const [type, id] = value.split(":")
  return (type === "application" || type === "component") && id
    ? { type, id }
    : undefined
}

/** What the inspector's adding or changing: a component, or a group. */
export type Editing =
  | {
      type: "component"
      kind: ComponentKind
      component?: Component
      groupId?: string
    }
  | { type: "group"; group?: Group }

/** What's being removed. */
export type Removing =
  | { type: "connection"; connection: Connection }
  | { type: "component"; component: Component }
  | { type: "group"; group: Group }

/** What a component is, in a few words: its kind and what it runs on. */
export const componentKind = (component: Component) =>
  [
    COMPONENT_KINDS[component.kind]?.label ?? component.kind,
    component.technology,
  ]
    .filter(Boolean)
    .join(" · ")
