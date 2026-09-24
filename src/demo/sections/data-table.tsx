import * as React from "react"

import { Button } from "@/components/ui/button"
import { ContextMenuItem } from "@/components/ui/context-menu"
import { toast } from "@/components/ui/toast"
import { PrimaryAction } from "@/components/forge/app-shell"
import { AgentAvatars, type Agent } from "@/components/forge/avatars"
import {
  DataTable,
  createColumnHelper,
  type DataTableOption,
  type PaginationState,
  type SortingState,
} from "@/components/forge/data-table"
import { PanelEmpty } from "@/components/forge/empty-state"
import { Icon } from "@/components/forge/icon"
import { StatusBadge } from "@/components/forge/status"
import { DateLabel, RepoLabel } from "@/components/forge/task-list"
import { DetailList, DetailRow } from "@/components/forge/task-sheet"
import { statusLabels, type TaskStatus } from "@/components/forge/variants"
import { agents, tasks } from "../data"
import {
  backlog as initialBacklog,
  createFiles,
  createJobs,
  formatBytes,
  jobStatuses,
  priorities,
  shifts,
  type BacklogItem,
  type FileNode,
  type Job,
  type Shift,
} from "../grid-data"
import { SectionPage, Specimen } from "../specimen"

type TaskRecord = {
  id: string
  title: string
  status: TaskStatus
  repository: string
  runs: number
  tokens: number
  updatedAt: number
  agents: Agent[]
}

const statuses: TaskStatus[] = [
  "pending",
  "enqueued",
  "running",
  "review",
  "completed",
  "failed",
  "cancelled",
]

const statusOptions: DataTableOption[] = statuses.map((status) => ({
  label: statusLabels[status],
  value: status,
}))

// 45 deterministic rows built from the sample tasks.
const records: TaskRecord[] = Array.from({ length: 45 }, (_, index) => {
  const base = tasks[index % tasks.length]
  return {
    id: `FG-${204 + index}`,
    title:
      index < tasks.length
        ? base.title
        : `${base.title} (${Math.floor(index / tasks.length) + 1})`,
    status: statuses[(index * 5) % statuses.length],
    repository: base.repository ?? "workspace/web",
    runs: (index % 4) + 1,
    tokens: 8_000 + ((index * 7_919) % 90_000),
    updatedAt: Date.UTC(
      2026,
      8,
      8 - (index % 9),
      9 + (index % 8),
      (index * 7) % 60
    ),
    agents: agents.slice(0, (index % 3) + 1),
  }
})

const dateFormat = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
})

const helper = createColumnHelper<TaskRecord>()

const columns = helper.columns([
  helper.accessor("title", {
    header: "Task",
    size: 260,
    meta: { label: "Task" },
    cell: ({ row, getValue }) => (
      <span className="flex min-w-0 items-center gap-[15px]">
        <span className="w-[52px] shrink-0 font-mono text-xs text-subtle">
          {row.original.id}
        </span>
        <span className="truncate text-[0.8125rem] font-[430]">
          {getValue()}
        </span>
      </span>
    ),
  }),
  helper.accessor("status", {
    header: "Status",
    size: 150,
    meta: {
      label: "Status",
      filterVariant: "multiSelect",
      filterOptions: statusOptions,
    },
    cell: ({ getValue }) => <StatusBadge status={getValue()} />,
  }),
  helper.accessor("repository", {
    header: "Repository",
    size: 180,
    meta: { label: "Repository", filterVariant: "select" },
    cell: ({ getValue }) => <RepoLabel>{getValue()}</RepoLabel>,
  }),
  helper.accessor("runs", {
    header: "Runs",
    size: 96,
    aggregationFn: "sum",
    meta: { label: "Runs", summary: "sum" },
    aggregatedCell: ({ getValue }) => (
      <span className="font-medium">{Number(getValue())}</span>
    ),
  }),
  helper.accessor("tokens", {
    header: "Tokens",
    size: 120,
    aggregationFn: "sum",
    meta: { label: "Tokens", format: "integer", summary: "sum" },
    aggregatedCell: ({ getValue }) => (
      <span className="font-medium">{Number(getValue()).toLocaleString()}</span>
    ),
  }),
  helper.accessor("updatedAt", {
    header: "Updated",
    size: 150,
    aggregationFn: "max",
    meta: { label: "Updated", filterVariant: "date" },
    cell: ({ getValue }) => (
      <DateLabel>{dateFormat.format(getValue())}</DateLabel>
    ),
    aggregatedCell: ({ getValue }) => (
      <DateLabel>Latest {dateFormat.format(Number(getValue()))}</DateLabel>
    ),
  }),
  helper.display({
    id: "agents",
    header: "Agents",
    size: 96,
    meta: { label: "Agents", align: "right" },
    cell: ({ row }) => <AgentAvatars agents={row.original.agents} />,
  }),
])

/* Spreadsheet ----------------------------------------------------------- */

const priorityOptions: DataTableOption[] = priorities.map((priority) => ({
  label: priority,
  value: priority,
}))
const jobStatusOptions: DataTableOption[] = jobStatuses.map((status) => ({
  label: statusLabels[status],
  value: status,
}))

const jobHelper = createColumnHelper<Job>()

const jobColumns = jobHelper.columns([
  jobHelper.accessor("id", {
    header: "ID",
    size: 104,
    enableHiding: false,
    meta: { label: "ID", className: "font-mono text-subtle" },
  }),
  jobHelper.group({
    id: "work",
    header: "Work",
    columns: jobHelper.columns([
      jobHelper.accessor("name", {
        header: "Name",
        size: 230,
        meta: {
          label: "Name",
          editable: true,
          validate: (value) =>
            String(value ?? "").trim() ? undefined : "Name can't be empty",
        },
      }),
      jobHelper.accessor("status", {
        header: "Status",
        size: 140,
        cell: ({ getValue }) => <StatusBadge status={getValue()} />,
        meta: {
          label: "Status",
          filterVariant: "multiSelect",
          filterOptions: jobStatusOptions,
          editable: true,
          editorOptions: jobStatusOptions,
        },
      }),
      jobHelper.accessor("priority", {
        header: "Priority",
        size: 96,
        meta: {
          label: "Priority",
          filterVariant: "select",
          editable: true,
          editorOptions: priorityOptions,
        },
      }),
      jobHelper.accessor("owner", {
        header: "Owner",
        size: 130,
        meta: { label: "Owner", filterVariant: "select" },
      }),
      jobHelper.accessor("team", {
        header: "Team",
        size: 116,
        meta: { label: "Team", filterVariant: "multiSelect" },
      }),
      jobHelper.accessor("billable", {
        header: "Billable",
        size: 92,
        meta: { label: "Billable", align: "center", editable: true },
      }),
    ]),
  }),
  jobHelper.group({
    id: "usage",
    header: "Usage",
    columns: jobHelper.columns([
      jobHelper.accessor("runs", {
        header: "Runs",
        size: 90,
        aggregationFn: "sum",
        meta: {
          label: "Runs",
          summary: "sum",
          editable: true,
          validate: (value) =>
            typeof value === "number" && value < 0
              ? "Must be 0 or more"
              : undefined,
        },
      }),
      jobHelper.accessor("tokens", {
        header: "Tokens",
        size: 116,
        aggregationFn: "sum",
        meta: {
          label: "Tokens",
          format: "integer",
          summary: "sum",
          editable: true,
        },
      }),
      jobHelper.accessor("cost", {
        header: "Cost",
        size: 110,
        aggregationFn: "sum",
        meta: {
          label: "Cost",
          format: "currency",
          summary: "sum",
          editable: true,
        },
      }),
      jobHelper.accessor("success", {
        header: "Success",
        size: 104,
        aggregationFn: "mean",
        meta: { label: "Success", format: "percent", summary: "mean" },
      }),
    ]),
  }),
  jobHelper.group({
    id: "schedule",
    header: "Schedule",
    columns: jobHelper.columns([
      jobHelper.accessor("due", {
        header: "Due",
        size: 130,
        meta: { label: "Due", format: "date", editable: true },
      }),
      jobHelper.accessor("updatedAt", {
        header: "Updated",
        size: 180,
        aggregationFn: "max",
        meta: { label: "Updated", format: "datetime", filterVariant: "date" },
      }),
    ]),
  }),
])

function SpreadsheetGrid() {
  const [jobs, setJobs] = React.useState(() => createJobs(5_000))
  return (
    <DataTable
      title="Agent jobs"
      description="5,000 rows, virtualized. Click and drag to select cells; double-click, Enter or start typing to edit."
      columns={jobColumns}
      data={jobs}
      getRowId={(job) => job.id}
      stateKey="demo-agent-jobs"
      exportFileName="agent-jobs"
      tableHeight="max-h-[560px]"
      density="compact"
      features={{
        cellSelection: true,
        virtualization: true,
        rowNumbers: true,
        footers: true,
        rowPinning: false,
      }}
      initialState={{ columnPinning: { start: ["id"], end: [] } }}
      onCellEdit={({ row, columnId, value }) =>
        setJobs((current) =>
          current.map((job) =>
            job.id === row.id ? { ...job, [columnId]: value } : job
          )
        )
      }
      contextMenuItems={({ row }) => (
        <ContextMenuItem
          onClick={() =>
            toast.add({ title: `Opened ${row.original.id}`, type: "info" })
          }
        >
          <Icon icon="external" className="text-muted-foreground" />
          Open {row.original.id}
        </ContextMenuItem>
      )}
      toolbarActions={
        <Button
          variant="ghost"
          size="sm"
          className="text-muted-foreground"
          onClick={() => setJobs(createJobs(5_000))}
        >
          Restore data
        </Button>
      }
    />
  )
}

const spreadsheetCode = `const columns = helper.columns([
  helper.accessor("id", { header: "ID", size: 104 }),
  helper.group({
    id: "work",
    header: "Work",
    columns: helper.columns([
      helper.accessor("name", {
        header: "Name",
        meta: { editable: true, validate: (v) => (v ? undefined : "Required") },
      }),
      helper.accessor("status", {
        header: "Status",
        meta: {
          filterVariant: "multiSelect",
          filterOptions: statusOptions,
          editable: true,
          editorOptions: statusOptions,
        },
      }),
      helper.accessor("billable", { header: "Billable", meta: { editable: true } }),
    ]),
  }),
  helper.group({
    id: "usage",
    header: "Usage",
    columns: helper.columns([
      helper.accessor("tokens", {
        header: "Tokens",
        meta: { format: "integer", summary: "sum", editable: true },
      }),
      helper.accessor("cost", {
        header: "Cost",
        meta: { format: "currency", summary: "sum" },
      }),
    ]),
  }),
])

<DataTable
  columns={columns}
  data={jobs}
  getRowId={(job) => job.id}
  stateKey="agent-jobs"            // remembers widths, order, pinning, sort
  features={{
    cellSelection: true,           // drag, shift-click, arrows, ⌘C / ⌘V
    virtualization: true,          // only visible rows render
    rowNumbers: true,
    footers: true,
  }}
  initialState={{ columnPinning: { start: ["id"], end: [] } }}
  onCellEdit={({ row, columnId, value }) =>
    setJobs((jobs) =>
      jobs.map((job) => (job.id === row.id ? { ...job, [columnId]: value } : job))
    )
  }
  contextMenuItems={({ row }) => (
    <ContextMenuItem onClick={() => open(row.original)}>Open</ContextMenuItem>
  )}
/>`

/* Tree data --------------------------------------------------------------- */

const fileHelper = createColumnHelper<FileNode>()

const fileColumns = fileHelper.columns([
  fileHelper.accessor("name", {
    header: "Name",
    size: 300,
    meta: { label: "Name" },
    cell: ({ row, getValue }) => (
      <span className="flex min-w-0 items-center gap-2">
        <Icon
          icon={row.original.kind === "folder" ? "folder" : "file"}
          size={15}
          className="shrink-0 text-subtle"
        />
        <span className="truncate">{getValue()}</span>
      </span>
    ),
  }),
  fileHelper.accessor("kind", {
    header: "Kind",
    size: 100,
    meta: { label: "Kind", filterVariant: "select" },
    cell: ({ getValue }) => (getValue() === "folder" ? "Folder" : "File"),
  }),
  fileHelper.accessor("owner", {
    header: "Owner",
    size: 140,
    meta: { label: "Owner", filterVariant: "select" },
  }),
  fileHelper.accessor("size", {
    header: "Size",
    size: 110,
    meta: {
      label: "Size",
      format: (value) => formatBytes(value),
    },
  }),
  fileHelper.accessor("modified", {
    header: "Modified",
    size: 180,
    meta: { label: "Modified", format: "datetime", filterVariant: "date" },
  }),
])

const files = createFiles()

/* Cell spanning ----------------------------------------------------------- */

const shiftHelper = createColumnHelper<Shift>()

const shiftColumns = shiftHelper.columns([
  shiftHelper.accessor("team", {
    header: "Team",
    size: 130,
    spanRows: true,
    meta: { label: "Team", className: "font-medium" },
  }),
  shiftHelper.accessor("service", {
    header: "Service",
    size: 160,
    meta: { label: "Service" },
  }),
  shiftHelper.accessor("primary", {
    header: "Primary",
    size: 140,
    spanRows: true,
    meta: { label: "Primary" },
  }),
  shiftHelper.accessor("secondary", {
    header: "Secondary",
    size: 140,
    meta: { label: "Secondary" },
  }),
  shiftHelper.accessor("window", {
    header: "Window",
    size: 120,
    spanRows: true,
    meta: { label: "Window" },
  }),
])

/* Row reordering ---------------------------------------------------------- */

const backlogHelper = createColumnHelper<BacklogItem>()

const backlogColumns = backlogHelper.columns([
  backlogHelper.accessor("title", {
    header: "Item",
    size: 320,
    meta: { label: "Item" },
  }),
  backlogHelper.accessor("owner", {
    header: "Owner",
    size: 140,
    meta: { label: "Owner" },
  }),
  backlogHelper.accessor("estimate", {
    header: "Points",
    size: 90,
    meta: { label: "Points" },
  }),
])

function BacklogGrid() {
  const [items, setItems] = React.useState(initialBacklog)
  return (
    <DataTable
      title="Backlog"
      description="Drag the handle to reprioritize."
      columns={backlogColumns}
      data={items}
      getRowId={(item) => item.id}
      features={{
        rowReordering: true,
        rowNumbers: true,
        rowPinning: false,
        rowSelection: false,
        pagination: false,
        columnFilters: false,
        statusBar: false,
      }}
      onRowReorder={({ row, targetRow, position }) =>
        setItems((current) => {
          const next = current.filter((item) => item.id !== row.id)
          const index = next.findIndex((item) => item.id === targetRow.id)
          next.splice(
            position === "before" ? index : index + 1,
            0,
            row.original
          )
          return next
        })
      }
    />
  )
}

/* Server-side ------------------------------------------------------------- */

type Query = {
  pagination: PaginationState
  sorting: SortingState
  search: string
}

// Stands in for an API: filters, sorts and pages on the "server".
function queryTasks({ pagination, sorting, search }: Query) {
  const term = search.trim().toLowerCase()
  const matching = records.filter(
    (record) =>
      !term ||
      record.title.toLowerCase().includes(term) ||
      record.repository.toLowerCase().includes(term)
  )
  const [sort] = sorting
  if (sort) {
    const key = sort.id as keyof TaskRecord
    matching.sort((a, b) => {
      const left = a[key]
      const right = b[key]
      const order =
        typeof left === "number" && typeof right === "number"
          ? left - right
          : String(left).localeCompare(String(right))
      return sort.desc ? -order : order
    })
  }
  const start = pagination.pageIndex * pagination.pageSize
  return {
    rows: matching.slice(start, start + pagination.pageSize),
    total: matching.length,
  }
}

function ServerSideGrid() {
  const [pagination, setPagination] = React.useState<PaginationState>({
    pageIndex: 0,
    pageSize: 10,
  })
  const [sorting, setSorting] = React.useState<SortingState>([])
  const [search, setSearch] = React.useState("")
  const request = JSON.stringify({ pagination, sorting, search })
  const [response, setResponse] = React.useState<{
    request: string
    rows: TaskRecord[]
    total: number
  }>({ request: "", rows: [], total: 0 })

  React.useEffect(() => {
    const timer = window.setTimeout(() => {
      setResponse({
        request,
        ...queryTasks({ pagination, sorting, search }),
      })
    }, 450)
    return () => window.clearTimeout(timer)
  }, [request, pagination, sorting, search])

  return (
    <DataTable
      title="Tasks (server)"
      description="Sorting, search and paging happen in a simulated API call."
      columns={columns}
      data={response.rows}
      isLoading={response.request !== request}
      features={{ columnFilters: false, grouping: false, rowPinning: false }}
      tableOptions={{
        manualPagination: true,
        manualSorting: true,
        manualFiltering: true,
        enableMultiSort: false,
        rowCount: response.total,
        state: { pagination, sorting, globalFilter: search },
        onPaginationChange: setPagination,
        onSortingChange: setSorting,
        onGlobalFilterChange: (updater) =>
          setSearch((current) => {
            const next =
              typeof updater === "function" ? updater(current) : updater
            return String(next ?? "")
          }),
      }}
    />
  )
}

const usage = `import { DataTable, createColumnHelper } from "@/components/forge/data-table"

const helper = createColumnHelper<Task>()
const columns = helper.columns([
  helper.accessor("title", { header: "Task", meta: { label: "Task" } }),
  helper.accessor("status", {
    header: "Status",
    cell: ({ getValue }) => <StatusBadge status={getValue()} />,
    meta: { filterVariant: "multiSelect", filterOptions: statusOptions },
  }),
  helper.accessor("repository", {
    header: "Repository",
    meta: { filterVariant: "select" },   // options faceted from the data
  }),
  helper.accessor("tokens", {
    header: "Tokens",
    meta: { format: "integer", summary: "sum" },   // range filter inferred
  }),
  helper.accessor("updatedAt", {
    header: "Updated",
    meta: { format: "date", filterVariant: "date" },
  }),
])

<DataTable
  title="Coding tasks"
  columns={columns}
  data={tasks}
  onRowClick={(row) => open(row.original.id)}
  renderSubComponent={(row) => <TaskDetails task={row.original} />}
  selectedRowsActions={({ selectedRows }) => (
    <Button size="sm" variant="outline">Cancel {selectedRows.length}</Button>
  )}
  tableHeight="max-h-[560px]"
/>`

export function DataTableSection() {
  const [loading, setLoading] = React.useState(true)
  return (
    <SectionPage
      icon="table"
      eyebrow="Workspace"
      title="Data table"
      description="A spreadsheet-grade grid on TanStack Table v9 in the workspace style: typed column filters, multi-sort, grouping with aggregation, column groups, pinning, resizing and drag to reorder or group; range selection with copy, paste and live statistics; inline editing with validation; virtualization for thousands of rows; tree data; cell spanning; row drag, pinning and selection; CSV export; a context menu; server-side data; and layouts that persist. Rows use the task list's metrics: 45px (34px compact), 11px muted headers, hairline dividers and quiet hover."
    >
      <Specimen
        title="Spreadsheet"
        description="Drag across cells or shift-click to select a range; the status bar shows count, sum, average, min and max. Arrow keys move, ⌘C copies, ⌘V pastes into editable cells, Delete clears them. Double-click or type to edit; Enter commits and moves down, Tab moves right, Esc cancels. Right-click for the menu. Drag a header onto the bar above the grid to group by it."
        variant="flush"
        className="border-0"
        code={spreadsheetCode}
      >
        <SpreadsheetGrid />
      </Specimen>

      <Specimen
        title="Full grid"
        description="Filters under each header match their data: multi-select for status, a faceted select for repository, number ranges for runs and tokens, a date range for updated. Hover a header for its menu (sort, group, move, pin, hide), drag it to reorder, drag its edge to resize (double-click resets). Shift-click headers to sort by several columns."
        variant="flush"
        className="border-0"
        code={usage}
      >
        <DataTable
          title="Coding tasks"
          description="Every task across the workspace, updated live."
          columns={columns}
          data={records}
          tableHeight="max-h-[560px]"
          onRowClick={(row) =>
            toast.add({ title: `Opened ${row.original.id}`, type: "info" })
          }
          renderSubComponent={(row) => (
            <DetailList className="mx-0 border-0 py-1">
              <DetailRow label="Task">{row.original.title}</DetailRow>
              <DetailRow label="Status">
                <StatusBadge status={row.original.status} />
              </DetailRow>
              <DetailRow label="Repository">
                {row.original.repository}
              </DetailRow>
              <DetailRow label="Agents">
                <span className="flex items-center gap-2">
                  <AgentAvatars agents={row.original.agents} />
                  {row.original.agents.map((agent) => agent.name).join(", ")}
                </span>
              </DetailRow>
            </DetailList>
          )}
          selectedRowsActions={({ selectedRows, table }) => (
            <>
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  toast.add({
                    title: `Requested cancellation for ${selectedRows.length} ${selectedRows.length === 1 ? "task" : "tasks"}.`,
                    type: "info",
                  })
                  table.resetRowSelection()
                }}
              >
                <Icon icon="stop" data-icon="inline-start" />
                Cancel runs
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="text-muted-foreground"
                onClick={() => table.resetRowSelection()}
              >
                Clear
              </Button>
            </>
          )}
          toolbarActions={<PrimaryAction>Task</PrimaryAction>}
        />
      </Specimen>

      <Specimen
        title="Tree data"
        description="Nested rows from getSubRows with indented expanders. Filtering keeps the parents of matching children, and selecting a folder selects its contents."
        variant="flush"
        className="border-0"
        code={`<DataTable
  columns={columns}
  data={files}
  getSubRows={(file) => file.children}
  initialState={{ expanded: true }}
  features={{ pagination: false, rowPinning: false }}
/>`}
      >
        <DataTable
          title="Repository files"
          columns={fileColumns}
          data={files}
          getSubRows={(file) => file.children}
          initialState={{ expanded: true }}
          features={{ pagination: false, rowPinning: false }}
          tableHeight="max-h-[480px]"
        />
      </Specimen>

      <Specimen
        title="Grouped, compact, with footers"
        description="Grouped by repository with summed runs and tokens, compact 34px rows and summary footers from meta.summary."
        variant="flush"
        className="border-0"
        code={`<DataTable
  columns={columns}
  data={tasks}
  density="compact"
  features={{ footers: true, rowPinning: false, columnFilters: false }}
  initialState={{ grouping: ["repository"], expanded: true }}
/>`}
      >
        <DataTable
          columns={columns}
          data={records.slice(0, 24)}
          density="compact"
          features={{ footers: true, rowPinning: false, columnFilters: false }}
          initialState={{
            grouping: ["repository"],
            expanded: true,
            pagination: { pageIndex: 0, pageSize: 30 },
          }}
          tableHeight="max-h-[460px]"
        />
      </Specimen>

      <Specimen
        title="Server-side"
        description="Manual pagination, sorting and search: the grid reports state changes and shows a loading overlay while the next page arrives."
        variant="flush"
        className="border-0"
        code={`<DataTable
  columns={columns}
  data={page.rows}
  isLoading={isFetching}
  tableOptions={{
    manualPagination: true,
    manualSorting: true,
    manualFiltering: true,
    rowCount: page.total,
    state: { pagination, sorting, globalFilter: search },
    onPaginationChange: setPagination,
    onSortingChange: setSorting,
    onGlobalFilterChange: setSearch,
  }}
/>`}
      >
        <ServerSideGrid />
      </Specimen>

      <div className="grid grid-cols-2 gap-6 @max-[1270px]/shell:grid-cols-1">
        <Specimen
          title="Cell spanning"
          description="spanRows merges equal neighbours down a column; range selection covers merged cells whole."
          variant="flush"
          className="border-0"
          code={`helper.accessor("team", { header: "Team", spanRows: true })

<DataTable
  columns={columns}
  data={shifts}
  features={{ cellSelection: true, pagination: false }}
/>`}
        >
          <DataTable
            title="On-call rota"
            columns={shiftColumns}
            data={shifts}
            getRowId={(shift) => shift.id}
            features={{
              cellSelection: true,
              pagination: false,
              rowSelection: false,
              rowPinning: false,
              columnFilters: false,
              sorting: false,
            }}
          />
        </Specimen>
        <Specimen
          title="Row reordering"
          description="Drag handles call onRowReorder; you decide the new order."
          variant="flush"
          className="border-0"
          code={`<DataTable
  columns={columns}
  data={items}
  features={{ rowReordering: true, rowNumbers: true }}
  onRowReorder={({ row, targetRow, position }) =>
    setItems((items) => move(items, row.id, targetRow.id, position))
  }
/>`}
        >
          <BacklogGrid />
        </Specimen>
      </div>

      <Specimen
        title="Minimal"
        description="Turn features off to get a plain sortable table."
        variant="flush"
        className="border-0"
        code={`<DataTable
  columns={columns}
  data={tasks}
  features={{
    toolbar: false, columnFilters: false, pagination: false,
    rowSelection: false, rowPinning: false, grouping: false,
    statusBar: false, contextMenu: false,
  }}
/>`}
      >
        <DataTable
          columns={columns}
          data={records.slice(0, 6)}
          features={{
            toolbar: false,
            columnFilters: false,
            pagination: false,
            rowSelection: false,
            rowPinning: false,
            grouping: false,
            statusBar: false,
            contextMenu: false,
          }}
          initialState={{ sorting: [{ id: "tokens", desc: true }] }}
        />
      </Specimen>

      <div className="grid grid-cols-2 gap-6 @max-[1270px]/shell:grid-cols-1">
        <Specimen
          title="Loading"
          variant="flush"
          className="border-0"
          code={`<DataTable columns={columns} data={[]} isLoading skeletonRows={4} />`}
        >
          <DataTable
            title="Coding tasks"
            columns={columns}
            data={loading ? [] : records.slice(0, 4)}
            isLoading={loading}
            skeletonRows={4}
            features={{ columnFilters: false, pagination: false }}
            toolbarActions={
              <Button
                variant="outline"
                size="sm"
                onClick={() => setLoading((value) => !value)}
              >
                {loading ? "Finish loading" : "Reload"}
              </Button>
            }
          />
        </Specimen>
        <Specimen
          title="Empty"
          variant="flush"
          className="border-0"
          code={`<DataTable
  columns={columns}
  data={[]}
  emptyState={<PanelEmpty illustration="tasks">No tasks yet.</PanelEmpty>}
/>`}
        >
          <DataTable
            title="Coding tasks"
            columns={columns}
            data={[]}
            features={{ columnFilters: false, pagination: false }}
            emptyState={
              <PanelEmpty illustration="tasks" className="py-6">
                No tasks yet. Create one to get started.
              </PanelEmpty>
            }
          />
        </Specimen>
      </div>
    </SectionPage>
  )
}
