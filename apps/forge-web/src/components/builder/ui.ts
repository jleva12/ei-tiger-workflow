import * as React from "react"

import type { IconProp } from "@/components/forge/icons"
import type { Lookups } from "@/lib/builder/adapter"
import type { BaseStep } from "@/lib/builder/types"
import type { WorkspaceView } from "@/lib/organization-workspace"

/*
 * What a builder shows that the kit can't know: what it builds and what
 * its steps are called, each kind's settings and the line summing a step
 * up on the canvas. The builder's page provides it around the kit's
 * components.
 */

/** A step's one line on the canvas, under its name. */
export type NodeSummary = {
  /** Plain words: what it does. */
  text?: string
  /** An expression or code: set in mono. */
  code?: string
  /** A short leading token: GET, 8 h. */
  tag?: string
}

/** The builder's words for what it builds, in its copy ("workflow", "step"). */
export type BuilderNouns = {
  doc: string
  docs: string
  step: string
  steps: string
}

/** A tint for a kind's glyph tile, by the group it's in. */
export type KindTone = "agent" | "action" | "logic"

export type BuilderUi = {
  nouns: BuilderNouns
  /** What it builds, as an icon: the canvas tab, its sidebar entry. */
  docIcon: IconProp
  /** The workspace page listing them, where the sidebar goes back to. */
  listView: WorkspaceView
  /** The permission saving needs, named when a save is refused. */
  permission: string
  /** Its kind's words under a step's name: how it's set up. */
  detailOf: (step: BaseStep, lookups: Lookups) => string
  /** A line on the step's card: what it does. */
  summaryOf: (step: BaseStep, lookups: Lookups) => NodeSummary | null
  /** The glyph a step wears, when it isn't its kind's. */
  iconOf?: (step: BaseStep) => IconProp
  /** A step's settings, in its dialog. */
  Fields: React.ComponentType<{ id: string; step: BaseStep }>
  /** Its Fields show the step's name themselves, so the dialog doesn't. */
  ownsName?: boolean
  /** Its steps' cards lift off the canvas with a shadow, not only when picked up. */
  raisedSteps?: boolean
  /**
   * Library groups whose kinds wear a tinted glyph tile, so a step's kind
   * reads at a glance. Agent orbs, the ends and a person's steps keep theirs.
   */
  kindTones?: Partial<Record<string, KindTone>>
  /** A step's card leads its second line with its kind ("HTTP request · 30 s timeout"). */
  kindLabels?: boolean
  /** Below the settings: how later steps read it, and what it can read. */
  DataSection?: React.ComponentType<{ id: string; step: BaseStep }>
  /** What the empty canvas suggests. */
  emptyCanvas: string
  /** What's ready says when nothing's wrong. */
  ready: string
}

export const BuilderUiContext = React.createContext<BuilderUi | null>(null)

export function useBuilderUi(): BuilderUi {
  const ui = React.useContext(BuilderUiContext)
  if (!ui) throw new Error("Builder components must be used inside a BuilderUiContext.")
  return ui
}

/** "workflow" → "Workflow". */
export const capitalized = (word: string) => word.charAt(0).toUpperCase() + word.slice(1)

/** "a workflow", "an agent". */
export const withArticle = (word: string) => `${/^[aeiou]/i.test(word) ? "an" : "a"} ${word}`
