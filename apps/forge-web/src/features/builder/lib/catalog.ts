import {
  AiBrain01Icon,
  AiProgrammingIcon,
  ApiIcon,
  ArrowRightDoubleIcon,
  BookOpen01Icon,
  Brain01Icon,
  ChatBotIcon,
  CommandLineIcon,
  DistributionIcon,
  FilterIcon,
  Flag02Icon,
  FlowSquareIcon,
  FunctionIcon,
  GitBranchPlusIcon,
  GitCommitIcon,
  GitForkIcon,
  GithubIcon,
  GitMergeIcon,
  GitPullRequestCreateIcon,
  Globe02Icon,
  HourglassIcon,
  LeftToRightListBulletIcon,
  LeftToRightListNumberIcon,
  McpServerIcon,
  PlayIcon,
  RepeatIcon,
  Robot01Icon,
  Route02Icon,
  SquareIcon,
  UserCheck01Icon,
  UserQuestion01Icon,
} from "@hugeicons/core-free-icons"

import type { IconProp } from "@/components/forge/icons"
import type { KindGroup, KindInfo, StepOutput } from "./types"

/*
 * A builder's kinds of node as the admin API serves them (a node catalog:
 * the workflow builder's, the Agents canvas's): each kind's words, its
 * library group, its ways out and what a new one starts with, in the
 * library's order. A builder installs its catalog once it's loaded, and
 * reads every kind from it; nothing knows them before. What only the
 * console has is here: each kind's icon, by the name the catalog gives it,
 * and which kinds the builder can set up (one it has no settings for is
 * left out).
 */

/** A way out of a kind's nodes, as served. */
export type CatalogOutput = {
  id: string
  label: string
  branch?: boolean
  fallback?: boolean
  body?: boolean
}

/** One kind of node, as served: what every catalog's kinds have. */
export type CatalogKind = {
  kind: string
  label: string
  group: string
  /** A Hugeicons name (`robot-01`): ICONS below. */
  icon: string
  summary: string
  keywords: string
  id_prefix: string
  orb?: number | null
  terminal?: boolean
  person?: boolean
  outputs: CatalogOutput[]
  /** The settings a new one starts with. */
  defaults: Record<string, unknown>
}

/** A node catalog, as served. */
export type Catalog<Entry extends CatalogKind = CatalogKind> = {
  groups: KindGroup[]
  kinds: Entry[]
}

/** A kind as the library, the canvas and the settings show it. */
export type CatalogKindInfo<Config = unknown> = KindInfo & {
  defaults: () => Config
}

/** Each icon a kind may name. One it doesn't know gets a plain square. */
const ICONS: Record<string, IconProp> = {
  play: PlayIcon,
  "chat-bot": ChatBotIcon,
  "robot-01": Robot01Icon,
  "ai-brain-01": AiBrain01Icon,
  "brain-01": Brain01Icon,
  "book-open-01": BookOpen01Icon,
  api: ApiIcon,
  "mcp-server": McpServerIcon,
  "left-to-right-list-number": LeftToRightListNumberIcon,
  distribution: DistributionIcon,
  repeat: RepeatIcon,
  "flow-square": FlowSquareIcon,
  "ai-programming": AiProgrammingIcon,
  "user-check-01": UserCheck01Icon,
  "user-question-01": UserQuestion01Icon,
  "globe-02": Globe02Icon,
  "command-line": CommandLineIcon,
  function: FunctionIcon,
  filter: FilterIcon,
  hourglass: HourglassIcon,
  "git-fork": GitForkIcon,
  "route-02": Route02Icon,
  "left-to-right-list-bullet": LeftToRightListBulletIcon,
  "arrow-right-double": ArrowRightDoubleIcon,
  "git-merge": GitMergeIcon,
  github: GithubIcon,
  "git-branch-plus": GitBranchPlusIcon,
  "git-commit": GitCommitIcon,
  "git-pull-request-create": GitPullRequestCreateIcon,
  "flag-02": Flag02Icon,
}

function infoOf(kind: CatalogKind): CatalogKindInfo {
  return {
    label: kind.label,
    group: kind.group,
    icon: ICONS[kind.icon] ?? SquareIcon,
    summary: kind.summary,
    keywords: kind.keywords,
    ...(kind.orb != null && { orb: kind.orb }),
    ...(kind.terminal && { terminal: true }),
    ...(kind.person && { person: true }),
    idPrefix: kind.id_prefix,
    defaults: () => structuredClone(kind.defaults),
  }
}

/** A kind's own ways out, as the canvas lists them. */
export const outputsOfKind = (kind: CatalogKind): StepOutput[] =>
  kind.outputs.map((output) => ({
    id: output.id,
    label: output.label,
    ...(output.branch && { branch: true }),
    ...(output.fallback && { fallback: true }),
    ...(output.body && { body: true }),
  }))

/**
 * A builder's node catalog, once installed: `name` says which in what it
 * throws, `setUp` is every kind the builder can set up. Reading a kind
 * before it's installed throws.
 */
export function kindCatalog<K extends string, Entry extends CatalogKind>(
  name: string,
  setUp: Record<K, true>
) {
  type Installed = {
    /** What was installed: installing it again changes nothing. */
    source: Catalog<Entry>
    groups: KindGroup[]
    /** The served kinds it can set up, in the library's order. */
    list: K[]
    infos: Record<string, CatalogKindInfo>
    served: Map<string, Entry>
  }
  let installed: Installed | null = null

  function current(): Installed {
    if (!installed)
      throw new Error(
        `The ${name} isn't loaded: render this inside NodeCatalogGate, or install it first.`
      )
    return installed
  }

  const isServed = (value: unknown): value is K =>
    typeof value === "string" && Object.hasOwn(current().infos, value)

  return {
    /** Every kind it can set up, served or not: the format's. */
    known: Object.keys(setUp) as K[],
    /** Makes `catalog` the one every kind is read from. */
    install(catalog: Catalog<Entry>): void {
      if (installed?.source === catalog) return
      const served = new Map(catalog.kinds.map((kind) => [kind.kind, kind]))
      const list = catalog.kinds
        .map((kind) => kind.kind)
        .filter((kind): kind is K => Object.hasOwn(setUp, kind))
      installed = {
        source: catalog,
        groups: catalog.groups,
        list,
        infos: Object.fromEntries(
          list.map((kind) => [kind, infoOf(served.get(kind)!)])
        ),
        served,
      }
    },
    installed: () => installed !== null,
    /** Whether a kind is served and can be set up. */
    isServed,
    /** A served kind as served, all it says. */
    entry(kind: K): Entry {
      const entry = isServed(kind) && current().served.get(kind)
      if (!entry) throw new Error(`The ${name} has no kind ${kind}`)
      return entry
    },
    /** A served kind, as the library, the canvas and the settings show it. */
    info(kind: K): CatalogKindInfo {
      const info = current().infos[kind]
      if (!info) throw new Error(`The ${name} has no kind ${kind}`)
      return info
    },
    /** Every served kind it can set up, in the library's order. */
    list: (): K[] => current().list,
    /** The same, by kind: what the builder kit reads. */
    infos: (): Record<string, CatalogKindInfo> => current().infos,
    /** The library's groups, in order. */
    groups: (): KindGroup[] => current().groups,
  }
}
