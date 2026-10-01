import type { IconSvgElement } from "@hugeicons/react"
import {
  Activity01Icon,
  Add01Icon,
  Alert02Icon,
  ArrowDown01Icon,
  ArrowLeft01Icon,
  ArrowRight01Icon,
  ArrowUp01Icon,
  ArrowUpRight01Icon,
  Calendar03Icon,
  Cancel01Icon,
  CancelCircleIcon,
  CheckmarkCircle02Icon,
  Clock01Icon,
  CodeIcon,
  Coins01Icon,
  Comment01Icon,
  CommandIcon,
  Copy01Icon,
  DashboardSquare01Icon,
  Download01Icon,
  DragDropVerticalIcon,
  File01Icon,
  FilterHorizontalIcon,
  Folder01Icon,
  GitBranchIcon,
  GitCommitIcon,
  GitPullRequestIcon,
  GridTableIcon,
  Home01Icon,
  InformationCircleIcon,
  KanbanIcon,
  Layers01Icon,
  Link01Icon,
  ListViewIcon,
  Loading03Icon,
  Message01Icon,
  Moon02Icon,
  MoreHorizontalIcon,
  Notification01Icon,
  PinIcon,
  PlusSignCircleIcon,
  RefreshIcon,
  Robot01Icon,
  Search01Icon,
  Settings01Icon,
  SidebarLeftIcon,
  Sorting01Icon,
  SparklesIcon,
  StopCircleIcon,
  Sun01Icon,
  Task01Icon,
  Tick02Icon,
  UserGroupIcon,
  ViewIcon,
} from "@hugeicons/core-free-icons"

/**
 * The Forge Workspace icon vocabulary: Hugeicons (free, stroke) rendered at
 * 17px with a 1.6 stroke. Names describe intent, not glyphs, so a product
 * can swap the underlying glyph without touching call sites.
 */
export const icons = {
  // Navigation
  home: Home01Icon,
  search: Search01Icon,
  bell: Notification01Icon,
  folder: Folder01Icon,
  task: Task01Icon,
  team: UserGroupIcon,
  dashboard: DashboardSquare01Icon,
  layers: Layers01Icon,
  settings: Settings01Icon,
  sidebar: SidebarLeftIcon,
  command: CommandIcon,
  // Views
  list: ListViewIcon,
  kanban: KanbanIcon,
  activity: Activity01Icon,
  calendar: Calendar03Icon,
  clock: Clock01Icon,
  // Theme
  sun: Sun01Icon,
  moon: Moon02Icon,
  // Direction
  down: ArrowDown01Icon,
  up: ArrowUp01Icon,
  left: ArrowLeft01Icon,
  right: ArrowRight01Icon,
  external: ArrowUpRight01Icon,
  // Actions
  plus: Add01Icon,
  addCircle: PlusSignCircleIcon,
  refresh: RefreshIcon,
  sort: Sorting01Icon,
  filter: FilterHorizontalIcon,
  close: Cancel01Icon,
  more: MoreHorizontalIcon,
  link: Link01Icon,
  copy: Copy01Icon,
  download: Download01Icon,
  view: ViewIcon,
  grip: DragDropVerticalIcon,
  table: GridTableIcon,
  pin: PinIcon,
  stop: StopCircleIcon,
  // Code
  branch: GitBranchIcon,
  commit: GitCommitIcon,
  review: GitPullRequestIcon,
  code: CodeIcon,
  file: File01Icon,
  // Agents and messages
  robot: Robot01Icon,
  message: Message01Icon,
  comment: Comment01Icon,
  sparkles: SparklesIcon,
  coins: Coins01Icon,
  // Status
  check: Tick02Icon,
  completed: CheckmarkCircle02Icon,
  failed: CancelCircleIcon,
  loading: Loading03Icon,
  info: InformationCircleIcon,
  warning: Alert02Icon,
} satisfies Record<string, IconSvgElement>

export type IconName = keyof typeof icons

/** An icon name from the Forge vocabulary, or any Hugeicons glyph object. */
export type IconProp = IconName | IconSvgElement

export const iconNames = Object.keys(icons) as IconName[]

export function resolveIcon(icon: IconProp): IconSvgElement {
  return typeof icon === "string" ? icons[icon] : icon
}
