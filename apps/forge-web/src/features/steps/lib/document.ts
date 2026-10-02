import type { Point } from "@/features/builder/lib/types"
import { uid, type StepData } from "./model"

/*
 * What an ADK workflow's and a chat agent's documents share about Forge's
 * steps: the shape of a graph of them, and how a list setting read from
 * JSON is made to fit.
 */

/** Forge's steps and the connections between them, as connections.ts reads them. */
export type StepGraph = {
  steps: { id: string; data: StepData; position: Point }[]
  connections: { source: string; output: string; target: string }[]
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value)

// What each item of a list setting looks like.
const ITEMS: Record<string, Record<string, string>> = {
  headers: { id: "", name: "", value: "" },
  cases: { id: "", value: "" },
  arms: { id: "", label: "", condition: "" },
}

/** A list setting (a switch's cases, a match's rules, headers…), each item made to fit. */
export function readList(key: string, value: unknown, path: string, notes: string[]) {
  const shape = ITEMS[key]
  if (!Array.isArray(value) || !shape) {
    notes.push(`${path} should be a list.`)
    return undefined
  }
  const ids = new Set<string>()
  return value.flatMap((item, index) => {
    if (!isRecord(item)) {
      notes.push(`${path}[${index}] isn't an object; dropped.`)
      return []
    }
    const row: Record<string, string> = {}
    for (const field of Object.keys(shape)) {
      row[field] = typeof item[field] === "string" ? (item[field] as string) : ""
    }
    if (!row.id || ids.has(row.id)) row.id = uid(key.replace(/s$/, ""))
    ids.add(row.id)
    return [row]
  })
}
