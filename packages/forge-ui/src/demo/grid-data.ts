import type { TaskStatus } from "@/components/forge/variants"

/** Small seeded PRNG so generated demo data is stable between reloads. */
function seeded(seed: number) {
  let state = seed
  return () => {
    state = (state + 0x6d2b79f5) | 0
    let t = Math.imul(state ^ (state >>> 15), 1 | state)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function pick<T>(random: () => number, items: readonly T[]) {
  return items[Math.floor(random() * items.length)]
}

/* Jobs: a large, editable spreadsheet ------------------------------------- */

export type Priority = "P0" | "P1" | "P2" | "P3"

export type Job = {
  id: string
  name: string
  owner: string
  team: string
  status: TaskStatus
  priority: Priority
  billable: boolean
  runs: number
  tokens: number
  cost: number
  success: number
  due: Date
  updatedAt: number
}

export const jobStatuses: TaskStatus[] = [
  "pending",
  "enqueued",
  "running",
  "review",
  "completed",
  "failed",
  "cancelled",
]

export const priorities: Priority[] = ["P0", "P1", "P2", "P3"]

const verbs = [
  "Migrate",
  "Refactor",
  "Audit",
  "Backfill",
  "Harden",
  "Profile",
  "Document",
  "Upgrade",
  "Index",
  "Deduplicate",
]
const subjects = [
  "billing webhooks",
  "session storage",
  "search indexer",
  "feature flags",
  "CI cache",
  "auth middleware",
  "export pipeline",
  "rate limiter",
  "notification queue",
  "schema registry",
  "image resizer",
  "audit log",
]
const owners = [
  "Ada Park",
  "Ben Ortiz",
  "Chloe Lin",
  "Dev Patel",
  "Eli Novak",
  "Farah Aziz",
  "Gus Moreau",
  "Hana Sato",
]
const teams = ["Platform", "Payments", "Growth", "Infra", "Data", "Mobile"]

export function createJobs(count: number): Job[] {
  const random = seeded(42)
  return Array.from({ length: count }, (_, index) => {
    const runs = 1 + Math.floor(random() * 24)
    const tokens = Math.round(2_000 + random() * 180_000)
    return {
      id: `JOB-${String(index + 1).padStart(4, "0")}`,
      name: `${pick(random, verbs)} ${pick(random, subjects)}`,
      owner: pick(random, owners),
      team: pick(random, teams),
      status: pick(random, jobStatuses),
      priority: pick(random, priorities),
      billable: random() > 0.35,
      runs,
      tokens,
      cost: Math.round(tokens * 0.0021 * 100) / 100,
      success: Math.round((0.55 + random() * 0.45) * 1000) / 1000,
      due: new Date(2026, 8, 1 + Math.floor(random() * 75)),
      updatedAt: Date.UTC(
        2026,
        8,
        1 + Math.floor(random() * 22),
        Math.floor(random() * 24),
        Math.floor(random() * 60)
      ),
    }
  })
}

/* Files: tree data ---------------------------------------------------------- */

export type FileNode = {
  name: string
  kind: "folder" | "file"
  owner: string
  size: number
  modified: number
  children?: FileNode[]
}

type Spec = [name: string, children?: Spec[]]

const fileSpec: Spec[] = [
  [
    "apps",
    [
      [
        "web",
        [
          ["package.json"],
          ["vite.config.ts"],
          [
            "src",
            [
              ["main.tsx"],
              ["App.tsx"],
              [
                "routes",
                [
                  ["tasks.tsx"],
                  ["runs.tsx"],
                  ["settings.tsx"],
                  ["billing.tsx"],
                ],
              ],
            ],
          ],
        ],
      ],
      ["worker", [["index.ts"], ["queue.ts"], ["sandbox.ts"]]],
    ],
  ],
  [
    "packages",
    [
      [
        "ui",
        [
          [
            "data-table",
            [
              ["body.tsx"],
              ["cells.tsx"],
              ["data-table.tsx"],
              ["filters.tsx"],
              ["header.tsx"],
              ["toolbar.tsx"],
            ],
          ],
          ["button.tsx"],
          ["dialog.tsx"],
          ["sheet.tsx"],
        ],
      ],
      ["config", [["eslint.config.js"], ["tsconfig.base.json"]]],
    ],
  ],
  ["README.md"],
  ["pnpm-lock.yaml"],
]

export function createFiles(): FileNode[] {
  const random = seeded(7)
  const build = ([name, children]: Spec): FileNode => {
    const nodes = children?.map(build)
    return {
      name,
      kind: nodes ? "folder" : "file",
      owner: pick(random, owners),
      size: nodes
        ? nodes.reduce((total, node) => total + node.size, 0)
        : Math.round(400 + random() * 48_000),
      modified: nodes
        ? Math.max(...nodes.map((node) => node.modified))
        : Date.UTC(
            2026,
            8,
            1 + Math.floor(random() * 22),
            Math.floor(random() * 24),
            Math.floor(random() * 60)
          ),
      children: nodes,
    }
  }
  return fileSpec.map(build)
}

export function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/* On-call rota: cell spanning -------------------------------------------- */

export type Shift = {
  id: string
  team: string
  service: string
  primary: string
  secondary: string
  window: string
}

export const shifts: Shift[] = [
  ["Platform", "API gateway", "Ada Park", "Eli Novak", "Mon–Wed"],
  ["Platform", "Auth", "Ada Park", "Gus Moreau", "Mon–Wed"],
  ["Platform", "Scheduler", "Dev Patel", "Gus Moreau", "Thu–Sun"],
  ["Payments", "Billing", "Farah Aziz", "Ben Ortiz", "Mon–Sun"],
  ["Payments", "Invoices", "Farah Aziz", "Ben Ortiz", "Mon–Sun"],
  ["Data", "Warehouse", "Hana Sato", "Chloe Lin", "Mon–Thu"],
  ["Data", "Pipelines", "Hana Sato", "Chloe Lin", "Fri–Sun"],
  ["Data", "Search", "Chloe Lin", "Hana Sato", "Fri–Sun"],
].map(([team, service, primary, secondary, window], index) => ({
  id: `shift-${index}`,
  team,
  service,
  primary,
  secondary,
  window,
}))

/* Backlog: row reordering -------------------------------------------------- */

export type BacklogItem = {
  id: string
  title: string
  estimate: number
  owner: string
}

export const backlog: BacklogItem[] = [
  ["Stream run logs over SSE", 5, "Ada Park"],
  ["Retry failed sandbox boots", 3, "Dev Patel"],
  ["Diff viewer: word-level highlights", 8, "Chloe Lin"],
  ["Cancel queued runs in bulk", 2, "Ben Ortiz"],
  ["Rate-limit agent tool calls", 5, "Eli Novak"],
  ["Pin tasks to the board", 1, "Hana Sato"],
].map(([title, estimate, owner], index) => ({
  id: `B-${index + 1}`,
  title: title as string,
  estimate: estimate as number,
  owner: owner as string,
}))
