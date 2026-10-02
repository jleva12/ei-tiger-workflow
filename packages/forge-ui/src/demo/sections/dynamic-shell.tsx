import { Button } from "@/components/ui/button"
import { Icon } from "@/components/forge/icon"
import { DynamicShellPreview } from "../dynamic-shell-preview"
import { CodeBlock, SectionPage, Specimen } from "../specimen"

export function DynamicShellSection() {
  return (
    <SectionPage
      icon="layers"
      eyebrow="Workspace"
      title="Dynamic shell"
      description="ShellProvider holds what the app shell shows — the icon rail, the sidebar sub nav and the header — and pages change it: each page brings its own sub nav, title, breadcrumbs, toolbar, actions and footer, and the shell goes back when the page unmounts."
    >
      <Specimen
        title="Pages drive the shell"
        description="The rail is set once. Tasks, Agents and Settings each bring a sub nav; Home hides it. Request review changes the page's state and the sidebar count follows; switching to Viewer hides the Agents area and the admin-only settings."
        variant="canvas"
        className="p-0 @max-[600px]/shell:p-0"
        code={`<ShellProvider
  initial={{ rail: { items: sections, footer: [settings] } }}
  currentPath={location.pathname}
  navigate={navigate}
>
  <ShellLayout brand={<Brand />}>
    <Routes />
  </ShellLayout>
</ShellProvider>`}
      >
        <div className="flex items-center justify-end border-b bg-background px-4 py-2.5">
          <Button
            variant="ghost"
            size="xs"
            className="text-muted-foreground"
            nativeButton={false}
            render={<a href="#dynamic-shell-app" />}
          >
            Open full screen
            <Icon icon="external" data-icon="inline-end" />
          </Button>
        </div>
        <div className="p-4">
          <div className="overflow-hidden rounded-(--radius-band) border bg-background shadow-(--shadow-float)">
            <DynamicShellPreview className="h-[640px] min-w-0" />
          </div>
        </div>
      </Specimen>

      <Specimen
        title="In a page"
        description="useShellPage (or <ShellPage />) sets the shell while the page is mounted; the slot components render the page's own JSX in the header, toolbar, footer and sidebar."
      >
        <CodeBlock
          code={`function TasksPage() {
  const [inReview, setInReview] = useState(2)

  useShellPage({
    header: {
      breadcrumbs: [{ label: "Tasks", icon: "task", href: "/tasks" }],
      title: "Needs review",
    },
    sidebar: {
      label: "Tasks",
      sections: [
        { id: "views", title: "Views", items: [
          { id: "all", label: "All tasks", icon: "list", href: "/tasks" },
          { id: "review", label: "Needs review", icon: "review",
            href: "/tasks/review", meta: inReview },  // follows state
        ]},
      ],
    },
  })

  return (
    <>
      <ShellHeaderActions>
        <Guard permission={["tasks", "create"]}>
          <PrimaryAction onClick={createTask}>Task</PrimaryAction>
        </Guard>
      </ShellHeaderActions>
      <ShellToolbar><ViewToolbar>…</ViewToolbar></ShellToolbar>
      <TaskList … />
    </>
  )
}

// From anywhere, and it stays when the page changes:
const shell = useShellApi()
shell.configure({ rail: { items: railFor(user) } })
shell.updateItem("inbox", { meta: unread })
shell.setActive({ rail: "tasks" })
const title = useShell((shell) => shell.header.title)`}
        />
      </Specimen>
    </SectionPage>
  )
}
