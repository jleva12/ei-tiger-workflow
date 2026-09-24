import {
  ComponentIcon,
  CursorPointer01Icon,
  GridTableIcon,
  PaintBoardIcon,
  SquareIcon,
  TableIcon,
  TextFontIcon,
} from "@hugeicons/core-free-icons"

import type { IconProp } from "@/components/forge/icons"

export const sectionGroups = [
  "Foundations",
  "Primitives",
  "Workspace",
  "Detail pages",
] as const

export type SectionGroup = (typeof sectionGroups)[number]

export const sections = [
  { id: "overview", label: "Overview", icon: "home", group: "Foundations" },
  { id: "colors", label: "Color", icon: PaintBoardIcon, group: "Foundations" },
  {
    id: "typography",
    label: "Typography",
    icon: TextFontIcon,
    group: "Foundations",
  },
  {
    id: "radii",
    label: "Radii & elevation",
    icon: SquareIcon,
    group: "Foundations",
  },
  { id: "icons", label: "Icons", icon: "sparkles", group: "Foundations" },
  {
    id: "preferences",
    label: "User preferences",
    icon: "settings",
    group: "Foundations",
  },
  {
    id: "access",
    label: "Roles & permissions",
    icon: "team",
    group: "Foundations",
  },
  {
    id: "buttons",
    label: "Buttons",
    icon: CursorPointer01Icon,
    group: "Primitives",
  },
  { id: "inputs", label: "Inputs & forms", icon: "file", group: "Primitives" },
  {
    id: "menus",
    label: "Menus & overlays",
    icon: "layers",
    group: "Primitives",
  },
  {
    id: "tabs",
    label: "Tabs & toggles",
    icon: "dashboard",
    group: "Primitives",
  },
  {
    id: "badges",
    label: "Badges & status",
    icon: "completed",
    group: "Primitives",
  },
  { id: "feedback", label: "Feedback", icon: "info", group: "Primitives" },
  { id: "shell", label: "App shell", icon: ComponentIcon, group: "Workspace" },
  {
    id: "dynamic-shell",
    label: "Dynamic shell",
    icon: "layers",
    group: "Workspace",
  },
  {
    id: "assistant",
    label: "AI assistant",
    icon: "sparkles",
    group: "Workspace",
  },
  {
    id: "navigation",
    label: "Navigation",
    icon: "sidebar",
    group: "Workspace",
  },
  { id: "task-list", label: "Task list", icon: "list", group: "Workspace" },
  {
    id: "data-table",
    label: "Data table",
    icon: GridTableIcon,
    group: "Workspace",
  },
  { id: "kanban", label: "Kanban board", icon: "kanban", group: "Workspace" },
  { id: "empty", label: "Empty states", icon: "task", group: "Workspace" },
  {
    id: "activity",
    label: "Activity & dashboard",
    icon: "activity",
    group: "Workspace",
  },
  { id: "sheets", label: "Sheets & forms", icon: "robot", group: "Workspace" },
  {
    id: "integrations",
    label: "Integrations",
    icon: "link",
    group: "Workspace",
  },
  {
    id: "task-page",
    label: "Task header & tabs",
    icon: "view",
    group: "Detail pages",
  },
  {
    id: "submission",
    label: "Submission",
    icon: "message",
    group: "Detail pages",
  },
  { id: "run-logs", label: "Run logs", icon: "code", group: "Detail pages" },
  {
    id: "changes",
    label: "Changes & diffs",
    icon: "review",
    group: "Detail pages",
  },
  {
    id: "handoff",
    label: "Handoff & verification",
    icon: "commit",
    group: "Detail pages",
  },
  {
    id: "metrics",
    label: "Metrics table",
    icon: TableIcon,
    group: "Detail pages",
  },
] as const satisfies readonly {
  id: string
  label: string
  icon: IconProp
  group: SectionGroup
}[]

export type SectionId = (typeof sections)[number]["id"]

export function isSectionId(value: string): value is SectionId {
  return sections.some((section) => section.id === value)
}
