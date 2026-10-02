import type { Agent } from "@/components/forge/avatars"
import type { DiffLine } from "@/components/forge/changes"
import type { KanbanColumnData } from "@/components/forge/kanban"
import type { TaskItem } from "@/components/forge/task-list"
import type { SymbolKind, TaskStatus } from "@/components/forge/variants"

export const agents: Agent[] = [
  { id: "coder", name: "Implementer", role: "implementer" },
  { id: "reviewer", name: "Code reviewer", role: "reviewer" },
  { id: "fixer", name: "Fixer", role: "fixer" },
]

const names = [
  "Build the task overview dashboard",
  "Add repository search and filtering",
  "Implement cursor-based pagination",
  "Connect real-time task updates",
  "Improve empty and error states",
  "Add agent plan selection",
  "Implement task cancellation",
  "Review API authentication flow",
  "Verify task retry behavior",
  "Audit keyboard navigation",
  "Test worker reconnection",
  "Review repository permissions",
  "Set up the admin API",
  "Define shared task contracts",
  "Configure the coding workflow",
]

const statuses: TaskStatus[] = [
  "pending",
  "enqueued",
  "enqueued",
  "pending",
  "enqueued",
  "running",
  "running",
  "review",
  "review",
  "review",
  "review",
  "review",
  "completed",
  "completed",
  "completed",
]

const repos = ["web", "admin-api", "coding-agent", "common"]

export const tasks: TaskItem[] = names.map((title, index) => ({
  id: `FG-${204 + index}`,
  title,
  status: statuses[index],
  repository: `workspace/${repos[index % 4]}`,
  updated: `Sep ${8 - (index % 5)}, 2026`,
  runCount: (index % 4) + 1,
  agents,
}))

export const groups: {
  id: string
  title: string
  symbol: SymbolKind
  statuses: TaskStatus[]
}[] = [
  {
    id: "backlog",
    title: "Backlog",
    symbol: "backlog",
    statuses: ["pending", "enqueued"],
  },
  {
    id: "progress",
    title: "In Progress",
    symbol: "progress",
    statuses: ["running"],
  },
  {
    id: "review",
    title: "Running · Reviewing",
    symbol: "review",
    statuses: ["review"],
  },
  {
    id: "completed",
    title: "Completed",
    symbol: "completed",
    statuses: ["completed"],
  },
]

export function groupTasks(items: TaskItem[]) {
  return groups.map((group) => ({
    ...group,
    tasks: items.filter((task) => group.statuses.includes(task.status)),
  }))
}

export const kanbanColumns: KanbanColumnData[] = groupTasks(tasks).map(
  (group) => ({
    id: group.id,
    title: group.title,
    symbol: group.symbol,
    tasks: group.tasks,
  })
)

export const events = [
  {
    type: "run_started",
    time: "Sep 8, 9:02 AM",
    message: "Implementer picked up the task on a dedicated branch.",
    reference: "FG-214",
  },
  {
    type: "tool_call",
    time: "Sep 8, 9:03 AM",
    message: "bash · npm test — 42 passed, 1 failed.",
    reference: "FG-214",
  },
  {
    type: "verification_failed",
    time: "Sep 8, 9:04 AM",
    message:
      "Harness verification failed via .forge.md steps: npm test exited with code 1.",
    reference: "FG-214",
    error: true,
  },
  {
    type: "review_requested",
    time: "Sep 8, 9:05 AM",
    message: "Code reviewer is inspecting the pinned changes.",
    reference: "FG-211",
  },
  {
    type: "run_completed",
    time: "Sep 7, 4:41 PM",
    message:
      "Approved with no remaining findings. Commit created on the task branch.",
    reference: "FG-216",
  },
]

export const diffLines: DiffLine[] = [
  { type: "hunk", content: "@@ -1,8 +1,18 @@" },
  {
    type: "context",
    old: 1,
    new: 1,
    content: "import { verifyToken } from '../lib/tokens'",
  },
  {
    type: "insert",
    new: 2,
    content: "import { UnauthorizedError } from '../lib/errors'",
  },
  { type: "context", old: 2, new: 3, content: "" },
  {
    type: "context",
    old: 3,
    new: 4,
    content: "export async function authenticate(request) {",
  },
  {
    type: "delete",
    old: 4,
    content: "  const token = request.headers.authorization",
  },
  { type: "delete", old: 5, content: "  return verifyToken(token)" },
  {
    type: "insert",
    new: 5,
    content: "  const header = request.headers.authorization",
  },
  {
    type: "insert",
    new: 6,
    content: "  if (!header?.startsWith('Bearer ')) {",
  },
  {
    type: "insert",
    new: 7,
    content: "    throw new UnauthorizedError('A bearer token is required')",
  },
  { type: "insert", new: 8, content: "  }" },
  { type: "insert", new: 9, content: "" },
  {
    type: "insert",
    new: 10,
    content: "  const token = header.slice(7).trim()",
  },
  { type: "insert", new: 11, content: "  if (!token) {" },
  {
    type: "insert",
    new: 12,
    content: "    throw new UnauthorizedError('Token cannot be empty')",
  },
  { type: "insert", new: 13, content: "  }" },
  { type: "insert", new: 14, content: "" },
  { type: "insert", new: 15, content: "  return await verifyToken(token)" },
  { type: "context", old: 6, new: 16, content: "}" },
  { type: "context", old: 7, new: 17, content: "" },
  { type: "context", old: 8, new: 18, content: "export default authenticate" },
]

export const logEntries = [
  {
    kind: "tool" as const,
    title: "edit",
    status: "completed",
    time: "09:03:11",
  },
  {
    kind: "tool" as const,
    title: "bash · npm test",
    status: "completed",
    time: "09:04:02",
  },
  {
    kind: "message" as const,
    title: "Agent output",
    time: "09:04:12",
    text: "The changes are in place and the tests pass. Preparing the implementation summary for review.",
    open: true,
  },
  {
    kind: "event" as const,
    title: "Harness verification · failed via .forge.md steps",
    status: "failed",
    time: "09:04:40",
    text: "[passed] npm ci (exit 0 · 6.4s)\n\n[failed] npm test (exit 1 · 1.3s)\n  × tests/auth.test.ts > rejects whitespace-only bearer tokens\n    AssertionError: promise resolved instead of rejecting\n\nTest output written into the worktree was reverted before review.",
    open: true,
  },
  {
    kind: "tool" as const,
    title: "git_review_diff",
    status: "running",
    time: "09:05:04",
    text: "Inspecting the pinned changes in src/middleware/auth.ts and tests/auth.test.ts.",
    open: true,
  },
]
