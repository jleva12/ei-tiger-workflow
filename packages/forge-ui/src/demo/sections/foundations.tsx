import * as React from "react"
import { cn } from "cn"

import { Button } from "@/components/ui/button"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
} from "@/components/ui/input-group"
import { useTheme } from "@/components/theme-provider"
import { Stat, StatGrid } from "@/components/forge/activity"
import { Icon } from "@/components/forge/icon"
import { iconNames } from "@/components/forge/icons"
import { StatusBadge, StatusSymbol } from "@/components/forge/status"
import { toneVars, type Tone } from "@/components/forge/variants"
import { Caption, CodeBlock, SectionPage, Specimen } from "../specimen"
import { sections, type SectionId } from "../nav"
import { registryConfig, registryEnv, registryRepo } from "../registry"

/* -------------------------------------------------------------------------- */
/* Overview                                                                   */
/* -------------------------------------------------------------------------- */

const principles = [
  {
    icon: "view" as const,
    title: "Make real state easy to scan",
    text: "Pastel status bands, compact badges and presence dots carry state; everything else stays quiet.",
  },
  {
    icon: "layers" as const,
    title: "Pale surfaces, restrained contrast",
    text: "Cool neutral surfaces (hue 260), hairline borders and one dark primary action per view.",
  },
  {
    icon: "list" as const,
    title: "Dense but readable",
    text: "13px body, 12px controls and 14px detail prose within a 75ch measure. Controls wrap rather than shrink.",
  },
  {
    icon: "info" as const,
    title: "Explicit feedback",
    text: "Loading, empty, connection failure and action results are always presented — never implied.",
  },
  {
    icon: "command" as const,
    title: "Keyboard first",
    text: "Visible 2px focus rings, labelled controls, keyboard-scrollable logs and shortcuts in the chrome.",
  },
  {
    icon: "clock" as const,
    title: "Motion only for state",
    text: "150ms colour transitions and spinners; reduced-motion preferences are honoured everywhere.",
  },
]

export function OverviewSection({
  onNavigate,
}: {
  onNavigate: (id: SectionId) => void
}) {
  const componentCount = sections.filter(
    (s) => s.group !== "Foundations"
  ).length
  return (
    <SectionPage
      icon="home"
      eyebrow="Forge Workspace design system"
      title="A quiet interface for dense, stateful work"
      description="The design language of the Forge coding-agent workspace, packaged as shadcn/ui components. Light product surfaces, compact type, pastel status bands and a dark primary action — ready to drop into any React + Tailwind v4 project."
    >
      <StatGrid className="grid-cols-4 @max-[800px]/shell:grid-cols-2">
        <Stat label="Component groups" value={componentCount} />
        <Stat label="Forge components" value="120+" />
        <Stat label="Icons" value={iconNames.length} />
        <Stat label="Color tokens" value="90+" />
      </StatGrid>

      <Specimen title="Principles" variant="flush">
        <div className="grid grid-cols-3 gap-px bg-border @max-[1050px]/shell:grid-cols-2 @max-[600px]/shell:grid-cols-1">
          {principles.map((item) => (
            <div key={item.title} className="bg-background p-5">
              <Icon icon={item.icon} className="mb-3 text-muted-foreground" />
              <h3 className="mb-1.5 text-[0.8125rem] font-[550]">
                {item.title}
              </h3>
              <p className="text-xs/[1.65] text-muted-foreground">
                {item.text}
              </p>
            </div>
          ))}
        </div>
      </Specimen>

      <Specimen
        title="Install in another project"
        description="The library is a shadcn registry. Point a project at it once, then add the theme and any component — dependencies (including the tuned shadcn primitives) come along."
      >
        <ol className="flex flex-col gap-5 text-xs/[1.6]">
          <li>
            <Caption>
              1 · Start from the same shadcn preset (Base UI, rhea, Hugeicons,
              Geist)
            </Caption>
            <CodeBlock code="npx shadcn@latest init --preset b27GdBA3 --base base" />
          </li>
          <li>
            <Caption>
              2 · Put a GitHub token that can read {registryRepo} in .env.local
            </Caption>
            <CodeBlock code={registryEnv} />
          </li>
          <li>
            <Caption>
              3 · Register the Forge registry in components.json
            </Caption>
            <CodeBlock code={registryConfig} />
          </li>
          <li>
            <Caption>
              4 · Add the theme, then everything (or pick components)
            </Caption>
            <CodeBlock
              code={`npx shadcn@latest add @forge-ui/theme
npx shadcn@latest add @forge-ui/all
# or: npx shadcn@latest add @forge-ui/app-shell @forge-ui/task-list @forge-ui/kanban`}
            />
          </li>
        </ol>
      </Specimen>

      <Specimen title="Explore" variant="canvas">
        <div className="grid grid-cols-3 gap-2.5 @max-[1050px]/shell:grid-cols-2 @max-[600px]/shell:grid-cols-1">
          {sections
            .filter((s) => s.id !== "overview")
            .map((section) => (
              <button
                key={section.id}
                type="button"
                onClick={() => onNavigate(section.id)}
                className="flex items-center gap-3 rounded-(--radius-card) border bg-background p-3.5 text-left transition-colors hover:bg-muted"
              >
                <span className="grid size-8 shrink-0 place-items-center rounded-(--radius-control) bg-muted text-muted-foreground">
                  <Icon icon={section.icon} size={16} />
                </span>
                <span className="min-w-0">
                  <span className="block text-[0.8125rem] font-[450]">
                    {section.label}
                  </span>
                  <span className="block truncate text-3xs text-subtle">
                    {section.group}
                  </span>
                </span>
              </button>
            ))}
        </div>
      </Specimen>
    </SectionPage>
  )
}

/* -------------------------------------------------------------------------- */
/* Colors                                                                     */
/* -------------------------------------------------------------------------- */

function useTokenValue(token: string) {
  const { theme } = useTheme()
  const [value, setValue] = React.useState("")
  React.useEffect(() => {
    const frame = requestAnimationFrame(() =>
      setValue(
        getComputedStyle(document.documentElement)
          .getPropertyValue(`--${token}`)
          .trim()
      )
    )
    return () => cancelAnimationFrame(frame)
  }, [token, theme])
  return value
}

function Swatch({ token, label }: { token: string; label?: string }) {
  const value = useTokenValue(token)
  return (
    <div className="min-w-0 overflow-hidden rounded-(--radius-card) border">
      <div
        className="h-14 border-b"
        style={{ background: `var(--${token})` }}
      />
      <div className="px-2.5 py-2">
        <div className="truncate text-xs font-medium">{label ?? token}</div>
        <code className="block truncate text-3xs text-muted-foreground">
          --{token}
        </code>
        <code className="block truncate text-4xs text-subtle">{value}</code>
      </div>
    </div>
  )
}

function SwatchGrid({ tokens }: { tokens: (string | [string, string])[] }) {
  return (
    <div className="grid grid-cols-6 gap-2.5 @max-[1270px]/shell:grid-cols-4 @max-[600px]/shell:grid-cols-2">
      {tokens.map((token) =>
        Array.isArray(token) ? (
          <Swatch key={token[0]} token={token[0]} label={token[1]} />
        ) : (
          <Swatch key={token} token={token} />
        )
      )}
    </div>
  )
}

const tones: {
  tone: Tone
  label: string
  symbol: "backlog" | "progress" | "review" | "completed" | "failed"
}[] = [
  { tone: "neutral", label: "Backlog", symbol: "backlog" },
  { tone: "amber", label: "In Progress", symbol: "progress" },
  { tone: "pink", label: "Running · Reviewing", symbol: "review" },
  { tone: "green", label: "Completed", symbol: "completed" },
  { tone: "red", label: "Failed", symbol: "failed" },
]

export function ColorsSection() {
  return (
    <SectionPage
      icon="sparkles"
      eyebrow="Foundations"
      title="Color"
      description="Cool neutrals at hue 260 do almost all of the work. Saturated color is reserved for state: group tones, status badges, run signals and feedback. Every value below is a CSS variable with a Tailwind utility (bg-subtle, text-status-running-foreground, …) and flips with the theme."
    >
      <Specimen
        title="Surfaces & text"
        description="shadcn semantic tokens tuned for the workspace, plus `subtle` for tertiary text and `focus` for the focus ring."
      >
        <SwatchGrid
          tokens={[
            "background",
            "foreground",
            "muted",
            "muted-foreground",
            "subtle",
            "nav-foreground",
            "border",
            "input",
            "primary",
            "primary-foreground",
            "accent",
            "focus",
          ]}
        />
      </Specimen>

      <Specimen
        title="Group tones"
        description="A pastel band surface and a saturated symbol color per status group. Children read them through --tone and --tone-foreground."
      >
        <div className="flex flex-col gap-2.5">
          {tones.map(({ tone, label, symbol }) => (
            <div
              key={tone}
              className={cn(
                toneVars[tone],
                "flex h-[39px] items-center gap-[9px] rounded-(--radius-band) bg-(--tone) px-[17px]"
              )}
            >
              <StatusSymbol kind={symbol} />
              <span className="font-mono text-xs font-[450]">{label}</span>
              <code className="ml-auto text-3xs text-muted-foreground">
                --tone-{tone} · --tone-{tone}-foreground
              </code>
            </div>
          ))}
        </div>
      </Specimen>

      <Specimen
        title="Task status"
        description="Badge surfaces dim with a brightness filter in dark mode instead of separate dark values."
      >
        <div className="grid grid-cols-6 gap-2.5 @max-[1050px]/shell:grid-cols-3 @max-[600px]/shell:grid-cols-2">
          {(
            [
              ["pending", "status-neutral"],
              ["enqueued", "status-queued"],
              ["running", "status-running"],
              ["review", "status-review"],
              ["completed", "status-success"],
              ["failed", "status-failed"],
            ] as const
          ).map(([status, token]) => (
            <div
              key={status}
              className="flex flex-col items-start gap-2 rounded-(--radius-card) border p-3"
            >
              <StatusBadge status={status} />
              <code className="text-4xs text-muted-foreground">--{token}</code>
            </div>
          ))}
        </div>
      </Specimen>

      <Specimen title="Feedback, notices & signals">
        <SwatchGrid
          tokens={[
            "danger-surface",
            "danger-foreground",
            "warning-surface",
            "warning-foreground",
            "success-surface",
            "success-foreground",
            "notice-surface",
            "notice-accent",
            "signal-running",
            "signal-success",
            "signal-failed",
            "online",
          ]}
        />
      </Specimen>

      <Specimen
        title="Console & diff"
        description="The log console is always dark; diff tints follow the theme."
      >
        <SwatchGrid
          tokens={[
            "console",
            "console-raised",
            "console-border",
            "console-foreground",
            "console-text",
            "console-muted",
            "console-success",
            "console-danger",
            "diff-added",
            "diff-removed",
            "diff-insert",
            "diff-delete",
          ]}
        />
      </Specimen>

      <Specimen title="Markers & avatars">
        <div className="flex flex-wrap items-center gap-6">
          {[1, 2, 3, 4].map((n) => (
            <div key={n} className="flex items-center gap-2 text-xs">
              <span
                className={cn(
                  "size-2 rounded-[3px]",
                  [
                    "bg-project-1",
                    "bg-project-2",
                    "bg-project-3",
                    "bg-project-4",
                  ][n - 1]
                )}
              />
              <code className="text-3xs text-muted-foreground">
                --project-{n}
              </code>
            </div>
          ))}
          <div className="flex items-center gap-2">
            <span className="size-[22px] rounded-(--radius-soft) bg-orb-workspace" />
            <code className="text-3xs text-muted-foreground">
              bg-orb-workspace
            </code>
          </div>
          <div className="flex items-center gap-2">
            {[
              "orb-agent-0",
              "orb-agent-1",
              "orb-agent-2",
              "orb-agent-3",
              "orb-agent-4",
            ].map((orb) => (
              <span key={orb} className={cn("size-[22px] rounded-full", orb)} />
            ))}
            <code className="text-3xs text-muted-foreground">
              orb-agent-0…4
            </code>
          </div>
        </div>
      </Specimen>
    </SectionPage>
  )
}

/* -------------------------------------------------------------------------- */
/* Typography                                                                 */
/* -------------------------------------------------------------------------- */

const typeScale = [
  {
    name: "Page title",
    spec: "26px · 550 · −0.7px",
    className: "text-[1.625rem] font-[550] tracking-[-.7px]",
    sample: "Test worker reconnection",
  },
  {
    name: "Display",
    spec: "23px · 500 · −0.65px",
    className: "text-[1.4375rem] font-medium tracking-[-.65px]",
    sample: "A clear space for your next idea",
  },
  {
    name: "View heading",
    spec: "21px · 500 · −0.5px",
    className: "text-[1.3125rem] font-medium tracking-[-.5px]",
    sample: "Workspace activity",
  },
  {
    name: "Section heading",
    spec: "text-lg · 18px · 550",
    className: "text-lg font-[550]",
    sample: "Execution plan",
  },
  {
    name: "UI / detail prose",
    spec: "text-sm · 14px · --text-ui",
    className: "text-sm",
    sample: "Implement the requested change and cover edge cases with tests.",
  },
  {
    name: "Body",
    spec: "13px (base) · 430",
    className: "text-[0.8125rem] font-[430]",
    sample: "Build the task overview dashboard",
  },
  {
    name: "Control",
    spec: "text-xs · 12px · 450",
    className: "text-xs font-[450]",
    sample: "Group by Status",
  },
  {
    name: "Caption",
    spec: "text-2xs · 11px",
    className: "text-2xs text-muted-foreground",
    sample: "Include the expected behavior and any useful context.",
  },
  {
    name: "Micro",
    spec: "text-3xs · 10px",
    className: "text-3xs text-subtle",
    sample: "Live updates on · 15 tasks",
  },
  {
    name: "Eyebrow",
    spec: "text-4xs · 9px · 0.15em caps",
    className: "text-4xs tracking-[.15em] text-subtle uppercase",
    sample: "Your coding workspace",
  },
  {
    name: "Mono",
    spec: "font-mono · 12px",
    className: "font-mono text-xs text-subtle",
    sample: "FG-214 · src/middleware/auth.ts",
  },
]

export function TypographySection() {
  return (
    <SectionPage
      icon="file"
      eyebrow="Foundations"
      title="Typography"
      description="Geist Variable throughout, with a compact scale: 13px body, 12px controls and 14px (--text-ui) for detail pages. Weights sit between the usual steps — 430, 450 and 550 — to keep dense lists calm. Monospace is reserved for identifiers, band titles and logs."
    >
      <Specimen title="Type scale" variant="flush">
        <div className="divide-y">
          {typeScale.map((row) => (
            <div
              key={row.name}
              className="grid grid-cols-[180px_minmax(0,1fr)] items-baseline gap-6 px-6 py-4 @max-[800px]/shell:grid-cols-1 @max-[800px]/shell:gap-1.5"
            >
              <div>
                <div className="text-xs font-medium">{row.name}</div>
                <code className="text-3xs text-muted-foreground">
                  {row.spec}
                </code>
              </div>
              <div className={cn("truncate", row.className)}>{row.sample}</div>
            </div>
          ))}
        </div>
      </Specimen>
      <Specimen title="Weights">
        <div className="flex flex-wrap gap-x-10 gap-y-4">
          {[400, 430, 450, 500, 550, 600].map((weight) => (
            <div key={weight}>
              <div className="text-2xl" style={{ fontWeight: weight }}>
                Aa
              </div>
              <code className="text-3xs text-muted-foreground">{weight}</code>
            </div>
          ))}
        </div>
      </Specimen>
    </SectionPage>
  )
}

/* -------------------------------------------------------------------------- */
/* Radii, borders & elevation                                                 */
/* -------------------------------------------------------------------------- */

const radii = [
  ["chip", "4px", "Badges, counts, chips"],
  ["item", "5px", "Menu items, file rows"],
  ["soft", "6px", "Job rows, page actions"],
  ["control", "7px", "Buttons, inputs, tabs, nav"],
  ["card", "8px", "Cards, table rows, console"],
  ["band", "9px", "Status bands, menus, toasts"],
  ["dialog", "16px", "Dialogs"],
] as const

const shadows = [
  ["raised", "Selected segment"],
  ["tab", "Active view tab"],
  ["paper", "Illustration sheet"],
  ["float", "Toasts & notices"],
] as const

export function RadiiSection() {
  return (
    <SectionPage
      icon="dashboard"
      eyebrow="Foundations"
      title="Radii, borders & elevation"
      description="Small, deliberate radii (4–9px), 1px neutral hairlines instead of shadows, and elevation used sparingly. Use the radius variables with Tailwind's variable shorthand: rounded-(--radius-control)."
    >
      <Specimen title="Radius tokens">
        <div className="grid grid-cols-7 gap-4 @max-[1270px]/shell:grid-cols-4 @max-[1050px]/shell:grid-cols-3 @max-[600px]/shell:grid-cols-2">
          {radii.map(([name, value, usage]) => (
            <div key={name}>
              <div
                className="mb-2.5 h-16 border bg-muted"
                style={{ borderRadius: `var(--radius-${name})` }}
              />
              <div className="text-xs font-medium">
                {name}{" "}
                <span className="font-normal text-muted-foreground">
                  {value}
                </span>
              </div>
              <code className="block text-3xs text-muted-foreground">
                rounded-(--radius-{name})
              </code>
              <div className="mt-1 text-3xs text-subtle">{usage}</div>
            </div>
          ))}
        </div>
      </Specimen>
      <Specimen title="Elevation" variant="canvas">
        <div className="grid grid-cols-5 gap-4 @max-[1050px]/shell:grid-cols-3 @max-[600px]/shell:grid-cols-2">
          {shadows.map(([name, usage]) => (
            <div key={name}>
              <div
                className="mb-2.5 h-16 rounded-(--radius-card) bg-background"
                style={{ boxShadow: `var(--shadow-${name})` }}
              />
              <code className="block text-3xs text-muted-foreground">
                shadow-(--shadow-{name})
              </code>
              <div className="mt-1 text-3xs text-subtle">{usage}</div>
            </div>
          ))}
        </div>
      </Specimen>
      <Specimen
        title="Borders & focus"
        description="Hairlines use --border everywhere. Focus is a 2px --focus outline offset by 3px on buttons, links and focusable regions — tab through these."
      >
        <div className="flex flex-wrap items-center gap-6">
          <div className="h-16 w-40 rounded-(--radius-card) border" />
          <div className="h-16 w-40 rounded-(--radius-card) border border-dashed" />
          <Button variant="outline">Focus me</Button>
          <a href="#radii" className="text-xs underline underline-offset-3">
            Focusable link
          </a>
          <div
            className="h-10 w-40 rounded-(--radius-control) border outline-2 outline-offset-3 outline-focus outline-solid"
            aria-hidden="true"
          />
        </div>
      </Specimen>
    </SectionPage>
  )
}

/* -------------------------------------------------------------------------- */
/* Icons                                                                      */
/* -------------------------------------------------------------------------- */

export function IconsSection() {
  const [query, setQuery] = React.useState("")
  const [copied, setCopied] = React.useState("")
  const shown = iconNames.filter((name) =>
    name.toLowerCase().includes(query.trim().toLowerCase())
  )
  return (
    <SectionPage
      icon="sparkles"
      eyebrow="Foundations"
      title="Icons"
      description="Hugeicons (free, stroke) at 17px with a 1.6 stroke. The workspace vocabulary maps intent names to glyphs; pass a name or any Hugeicons object. Inside buttons, menus and tabs the control sizes the icon. Click an icon to copy its usage."
    >
      <Specimen
        title={`Vocabulary · ${shown.length}`}
        code={`import { Icon } from "@/components/forge/icon"
import { Robot01Icon } from "@hugeicons/core-free-icons"

<Icon icon="branch" />              // by name
<Icon icon={Robot01Icon} size={15} /> // any Hugeicons glyph
<Button><Icon icon="plus" data-icon="inline-start" />Create</Button>`}
      >
        <InputGroup className="mb-5 max-w-72">
          <InputGroupAddon>
            <Icon icon="search" />
          </InputGroupAddon>
          <InputGroupInput
            aria-label="Filter icons"
            placeholder="Filter icons…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            className="text-xs md:text-xs"
          />
        </InputGroup>
        <div className="grid grid-cols-8 gap-1.5 @max-[1270px]/shell:grid-cols-6 @max-[800px]/shell:grid-cols-4 @max-[600px]/shell:grid-cols-3">
          {shown.map((name) => (
            <button
              key={name}
              type="button"
              onClick={() => {
                void navigator.clipboard
                  .writeText(`<Icon icon="${name}" />`)
                  .then(() => {
                    setCopied(name)
                    setTimeout(() => setCopied(""), 1200)
                  })
              }}
              className="flex flex-col items-center gap-2 rounded-(--radius-card) border border-transparent px-2 py-4 text-muted-foreground transition-colors hover:border-border hover:bg-muted hover:text-foreground"
            >
              <Icon icon={copied === name ? "check" : name} />
              <span className="max-w-full truncate font-mono text-3xs">
                {copied === name ? "copied" : name}
              </span>
            </button>
          ))}
        </div>
      </Specimen>
    </SectionPage>
  )
}
