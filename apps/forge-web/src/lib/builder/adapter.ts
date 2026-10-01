import type { ConnectionMeaning, EdgeTone } from "./connections"
import type { Measure } from "./layout"
import type {
  BaseStep,
  BuilderGraph,
  BuilderIssue,
  KindGroup,
  KindInfo,
  Point,
  StepOutput,
} from "./types"

/*
 * What a builder (the workflow builder, the agent builder) tells the
 * builder kit about what it builds: its kinds of step, how a step is made,
 * copied and connected, how its document becomes the graph the canvas
 * edits and back, how the graph is checked, and how it's laid out and
 * drawn. The kit's store, canvas, library, picker and step dialog work
 * from this alone; each builder's own settings and summaries come from its
 * `BuilderUi` (components/builder/ui).
 *
 * Members are methods, so an adapter for one kind of step fits where the
 * kit holds any.
 */

/** Who a builder's document belongs to and what it's called, as the store keeps it. */
export type BaseMeta = {
  id: string
  name: string
  description: string
  organization_id: string
  created_at: string
  updated_at: string
}

/** Names for the IDs steps hold (other documents, models), to show them by name. */
export type Lookups = Record<string, Record<string, string>>

export type BuilderAdapter<
  S extends BaseStep = BaseStep,
  Doc = unknown,
  Context = unknown,
  Scope = unknown,
> = {
  /** Every kind, by its key. */
  kinds: Record<string, KindInfo>
  /** The kinds, in the library's order. */
  kindList: string[]
  groups: KindGroup[]
  isKind(value: unknown): value is S["kind"]
  /** A new step of a kind; `steps` are the ones already there (names stay unique). */
  newStep(kind: S["kind"], steps: S[]): S
  /** A new step's ID, unlike any taken. */
  nextStepId(kind: S["kind"], taken: Iterable<string>): string
  /** A copy of a step, beside the ones already there. */
  copyStep(step: S, steps: S[]): S
  /** A step's ways out, in the order the canvas lists them. */
  outputsOf(step: S): StepOutput[]
  /** Whether a kind has a way in (the start has none). */
  hasInput(kind: S["kind"]): boolean
  /**
   * Whether one of a step's ways out may lead to a step of a kind (an
   * agent hands off only to agents). Without it, any way out may lead to
   * any kind with a way in.
   */
  accepts?(step: S, output: string, kind: S["kind"]): boolean
  /** Whether a graph has at most one of a kind (the start). */
  unique(kind: S["kind"]): boolean
  /** Whether a kind can go in between two steps: it has a way in and out. */
  insertable(kind: S["kind"]): boolean
  /** Whether a step has a name of its own to edit (the start doesn't). */
  named(kind: S["kind"]): boolean
  metaOf(doc: Doc): BaseMeta
  toGraph(doc: Doc): BuilderGraph<S>
  toDocument(meta: BaseMeta, graph: BuilderGraph<S>): Doc
  /** What's wrong, and what each step can read: worked out once per change. */
  check(graph: BuilderGraph<S>, context: Context): {
    issues: BuilderIssue[]
    scopeOf: (id: string) => Scope
  }
  /** Tidy up. */
  tidy(graph: BuilderGraph<S>, measure: Measure): Map<string, Point>
  /** What each connection means, for drawing it. */
  meaningsOf(
    graph: BuilderGraph<S>,
    edgeId: (c: BuilderGraph<S>["connections"][number]) => string
  ): Map<string, ConnectionMeaning>
  /** Each loop's body, by the loop's ID. */
  loopBodies(graph: BuilderGraph<S>): Map<string, Set<string>>
  /** The tone a way out's handle wears. */
  toneOf(step: S, output: StepOutput, index: number): EdgeTone
  /** The lookups a builder starts with, before any are loaded. */
  lookups(): Lookups
}
