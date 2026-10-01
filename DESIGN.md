---
name: Forge
description: A quiet control room for organizations running business workflows with people and AI agents; calm cool paper, graphite ink, and color spent only on state.
colors:
  primary: "oklch(0.19 0.006 260)"
  primary-foreground: "oklch(0.985 0.003 260)"
  background: "oklch(0.998 0.002 260)"
  foreground: "oklch(0.235 0.009 260)"
  muted: "oklch(0.97 0.003 260)"
  muted-foreground: "oklch(0.53 0.014 260)"
  nav-foreground: "oklch(0.42 0.012 260)"
  subtle: "oklch(0.64 0.012 260)"
  border: "oklch(0.925 0.004 260)"
  focus: "oklch(0.65 0.12 260)"
  destructive: "oklch(0.577 0.245 27.325)"
  danger-surface: "oklch(0.97 0.018 25)"
  danger-border: "oklch(0.89 0.045 25)"
  danger-foreground: "oklch(0.45 0.13 25)"
  online: "oklch(0.66 0.13 157)"
  status-neutral: "oklch(0.956 0.003 260)"
  status-neutral-foreground: "oklch(0.43 0.012 260)"
  status-queued: "oklch(0.934 0.033 256)"
  status-queued-foreground: "oklch(0.5 0.14 256)"
  status-running: "oklch(0.96 0.058 88)"
  status-running-foreground: "oklch(0.51 0.115 64)"
  status-review: "oklch(0.952 0.034 340)"
  status-review-foreground: "oklch(0.53 0.15 340)"
  status-success: "oklch(0.937 0.058 164)"
  status-success-foreground: "oklch(0.46 0.095 164)"
  status-failed: "oklch(0.936 0.036 20)"
  status-failed-foreground: "oklch(0.5 0.17 25)"
  tone-neutral: "oklch(0.972 0.004 260)"
  tone-neutral-foreground: "oklch(0.43 0.014 260)"
  tone-amber: "oklch(0.98 0.016 80)"
  tone-amber-foreground: "oklch(0.67 0.16 54)"
  tone-pink: "oklch(0.976 0.014 335)"
  tone-pink-foreground: "oklch(0.68 0.17 347)"
  tone-green: "oklch(0.978 0.022 155)"
  tone-green-foreground: "oklch(0.61 0.15 155)"
  tone-red: "oklch(0.977 0.015 25)"
  tone-red-foreground: "oklch(0.62 0.2 25)"
  signal-running: "#bc9229"
  signal-success: "#289b50"
  signal-failed: "#ce5555"
  notice-surface: "#fbfaf6"
  notice-border: "#e9e6dd"
  notice-foreground: "#807b6f"
  notice-accent: "#9b813b"
  notice-chip-foreground: "#857345"
  diff-added: "#218449"
  diff-removed: "#c04c50"
  diff-insert: "#e6ffec"
  diff-delete: "#ffebe9"
  console: "#151920"
  console-raised: "#1b2029"
  console-border: "#303642"
  console-foreground: "#d4d9e2"
  console-text: "#aeb8c9"
  console-muted: "#8b96a8"
  chart-1: "oklch(0.33 0.012 260)"
  chart-2: "oklch(0.55 0.014 260)"
  chart-3: "oklch(0.74 0.012 260)"
  chart-4: "oklch(0.86 0.008 260)"
  chart-5: "oklch(0.93 0.005 260)"
  chart-positive: "#23874a"
  chart-neutral: "#a9aeb8"
  chart-negative: "#c94f4f"
  viz-1: "#3d6fd9"
  viz-2: "#159a8a"
  viz-3: "#e0932b"
  viz-4: "#d6558c"
  viz-5: "#7b5cd6"
  viz-ramp-1: "#2a4fa6"
  viz-ramp-2: "#3d6fd9"
  viz-ramp-3: "#6590e8"
  viz-ramp-4: "#88abef"
typography:
  display:
    fontFamily: "Geist Variable, sans-serif"
    fontSize: "1.625rem"
    fontWeight: 550
    lineHeight: 1.3
    letterSpacing: "-0.7px"
  headline:
    fontFamily: "Geist Variable, sans-serif"
    fontSize: "1.3125rem"
    fontWeight: 500
    lineHeight: 1.5
    letterSpacing: "-0.5px"
  title:
    fontFamily: "Geist Variable, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 500
    lineHeight: 1.5
  body-ui:
    fontFamily: "Geist Variable, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
  body:
    fontFamily: "Geist Variable, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "Geist Variable, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 450
    lineHeight: 1.5
  label-caps:
    fontFamily: "Geist Variable, sans-serif"
    fontSize: "0.6875rem"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "0.025em"
  micro:
    fontFamily: "Geist Variable, sans-serif"
    fontSize: "0.625rem"
    fontWeight: 450
    lineHeight: 1.2
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace"
    fontSize: "0.75rem"
    fontWeight: 450
    lineHeight: 1.5
rounded:
  chip: "4px"
  item: "5px"
  soft: "6px"
  control: "7px"
  card: "8px"
  band: "9px"
  dialog: "16px"
spacing:
  unit: "0.25rem"
  unit-compact: "0.21875rem"
  row: "45px"
  row-compact: "34px"
  page-gutter: "22px"
  page-gutter-narrow: "13px"
  sheet-inset: "12px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-foreground}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    height: "32px"
    padding: "0 12px"
  button-primary-hover:
    backgroundColor: "color-mix(in oklab, oklch(0.19 0.006 260) 80%, transparent)"
  button-outline:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    height: "32px"
    padding: "0 12px"
  button-outline-hover:
    backgroundColor: "{colors.muted}"
  button-ghost-hover:
    backgroundColor: "{colors.muted}"
    textColor: "{colors.foreground}"
  button-destructive:
    backgroundColor: "color-mix(in oklab, oklch(0.577 0.245 27.325) 10%, transparent)"
    textColor: "{colors.destructive}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    height: "32px"
    padding: "0 12px"
  input:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    typography: "{typography.body-ui}"
    rounded: "{rounded.control}"
    height: "32px"
    padding: "4px 10px"
  nav-item:
    textColor: "{colors.nav-foreground}"
    typography: "{typography.body-ui}"
    rounded: "{rounded.control}"
    height: "36px"
    padding: "7px 10px"
  nav-item-active:
    backgroundColor: "{colors.muted}"
    textColor: "{colors.foreground}"
  status-badge-running:
    backgroundColor: "{colors.status-running}"
    textColor: "{colors.status-running-foreground}"
    typography: "{typography.micro}"
    rounded: "{rounded.chip}"
    height: "19px"
    padding: "2px 7px"
  status-band:
    backgroundColor: "{colors.tone-amber}"
    textColor: "{colors.muted-foreground}"
    rounded: "{rounded.band}"
    height: "39px"
    padding: "0 12px 0 17px"
  task-row:
    textColor: "{colors.foreground}"
    typography: "{typography.label}"
    height: "45px"
    padding: "0 17px"
  menu:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.band}"
    padding: "4px"
  sheet:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.dialog}"
  waiting-notice:
    backgroundColor: "{colors.notice-surface}"
    textColor: "{colors.notice-foreground}"
    rounded: "{rounded.card}"
    padding: "20px"
  log-console:
    backgroundColor: "{colors.console}"
    textColor: "{colors.console-foreground}"
    rounded: "{rounded.card}"
  chart-tooltip:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    typography: "{typography.label}"
    rounded: "{rounded.band}"
    padding: "8px 10px"
  ledger-row-selected:
    backgroundColor: "{colors.muted}"
    textColor: "{colors.foreground}"
  handoff-key:
    backgroundColor: "{colors.notice-surface}"
    textColor: "{colors.notice-chip-foreground}"
    typography: "{typography.micro}"
    rounded: "{rounded.chip}"
    height: "19px"
    padding: "0 5px"
  workflow-step:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    typography: "{typography.label}"
    rounded: "{rounded.card}"
    width: "15rem"
    padding: "10px 12px"
  workflow-step-approval:
    backgroundColor: "{colors.notice-surface}"
    textColor: "{colors.notice-foreground}"
    rounded: "{rounded.card}"
  workflow-branch-row:
    textColor: "{colors.muted-foreground}"
    height: "28px"
    padding: "0 16px 0 12px"
  workflow-terminal-tile:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-foreground}"
    rounded: "{rounded.soft}"
    size: "28px"
  workflow-details:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    width: "22rem"
  workflow-step-dialog:
    backgroundColor: "{colors.popover}"
    textColor: "{colors.popover-foreground}"
    rounded: "{rounded.dialog}"
    width: "clamp(34rem, 34vw, 52rem)"
  expression-field:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    typography: "{typography.mono}"
    rounded: "{rounded.control}"
    height: "32px"
    padding: "5px 10px"
  expression-template-chip:
    backgroundColor: "{colors.muted}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.chip}"
    padding: "1px 3px"
  expression-completion-item:
    textColor: "{colors.foreground}"
    rounded: "{rounded.item}"
    padding: "5px 8px"
  expression-completion-item-selected:
    backgroundColor: "{colors.muted}"
    textColor: "{colors.foreground}"
  schema-field-row:
    textColor: "{colors.foreground}"
    typography: "{typography.mono}"
    padding: "6px 10px"
---

# Design System: Forge

## Overview

**Creative North Star: "The Quiet Control Room"**

Forge is where an organization watches its workflows run and steps in when a person is needed. The room is kept calm on purpose. Surfaces are Cool Paper, a near-white faintly tinted toward blue (hue 260), with Deep Graphite text and hairline borders. Nothing in the chrome asks for attention. Color appears only when something has a state worth knowing: a band of pastel amber for work in progress, pink for work in review, green for done, red for failed. There is one dark control per view, the Control Ink primary action. The log console is the one dark room in the building, where the machines talk.

The mood is **calm, precise and unhurried**. Urgency comes from state, never from styling. Type is small and exact: 13px body, 12px controls, 14px for the text you navigate by, and larger sizes only on the titles of pages and records. Density is a product feature, not an accident. Rows are 45px, or 34px in compact density, and every size is in rem so the person's text-size preference reaches everything. Motion only reports a change of state, and it switches off when the person or their OS asks.

Forge rejects four looks by name. It is not a **neon "AI" product**: no dark-by-default screens, glowing gradients or purple-blue sparkle; agents are shown working, not as spectacle. It is not a **heavy enterprise dashboard**: no saturated blue chrome, thick borders, cards boxing every region, or big KPI tiles. It is not a **playful consumer app**: no big radii, bouncy motion, oversized illustrations or bubbly type. It is not **terminal cosplay**: monospace and dark surfaces belong to IDs, code and logs, not to the interface around them.

**Key Characteristics:**
- Cool Paper surfaces and Deep Graphite ink, all on hue 260 at very low chroma.
- Color is spent only on state: pastel surfaces paired with a same-hue ink.
- One Control Ink primary action per view.
- Compact rem-based type (13px body) in a single sans family, Geist.
- Flat until it floats: hairlines and tone shifts at rest, shadows only on overlays.
- Small radii chosen by role (4–9px), with 16px reserved for dialogs and sheets.
- An always-dark log console as the single dark surface in light mode.
- Density, text size, contrast and motion follow the person's preferences.

## Colors

The palette is a near-monochrome cool paper-and-graphite field. Pastel status pairs and a few precise signal colors are the only chroma. Frontmatter keys match the CSS custom properties in `apps/forge-web/src/index.css`, and through them the Tailwind utilities (`bg-status-running`, `text-subtle`).

### Primary
- **Control Ink** (primary): the fill of the single primary action in a view, such as a topbar's create button or a sheet's submit. In dark theme it inverts to a pale ink on a graphite page.
- **Paper on Ink** (primary-foreground): the text and icons on Control Ink.

### Neutral
- **Cool Paper** (background): the page, sidebar, cards, menus, popovers and sheets. It is one surface everywhere, and regions are told apart by hairlines, not by fills.
- **Deep Graphite** (foreground): primary text and active navigation.
- **Pale Mist** (muted): hover and active fills on rows, nav items, ghost and outline buttons, and the secondary button's fill.
- **Slate Secondary** (muted-foreground): descriptions, table headers and secondary text.
- **Nav Slate** (nav-foreground): resting sidebar navigation text. It sits between Slate Secondary and Deep Graphite, so the current page reads as darker.
- **Faint Slate** (subtle): the quietest text, for hints, timestamps, placeholders, counts and sidebar section labels.
- **Hairline** (border): every divider, input stroke, outline button and region edge.
- **Focus Blue** (focus): the global 2px focus outline. It is the only chromatic color in the chrome and appears only for keyboard focus.
- **Destructive Red** (destructive), with **Danger Surface / Border / Ink** (danger-*): destructive actions appear as a tint (10% fill with red text), never as a solid red block. Inline errors and validation use the danger trio.
- **Presence Green** (online): the small connection dot, such as "Admin API connected" in the footer.

### Status Signals
- **Status pairs** (status-*): the badge surfaces for task states, each with its own darker ink of the same hue. Neutral covers pending and cancelled, soft blue is queued, butter amber is running, rose is review, mint is completed, and blush is failed.
- **Group tones** (tone-*): the fainter surfaces of the full-width group bands and kanban columns, each paired with a saturated symbol color for the ring, dot or check at the band's start. They come in neutral, amber, pink, green and red.
- **Run signals** (signal-*): the icon colors for a job or step that is running (ochre), succeeded (green) or failed (red).
- **Waiting Notice** (notice-*): a warm-paper exception to the cool field. It is a slightly yellowed surface with an antique-gold accent, used only for "waiting on you / waiting on an agent" notices.
- **Diff** (diff-*): pale green and red line fills for inserted and deleted lines, with deeper green and red for the +/− marks and counts.

### Console
- **Console Night** (console, console-raised, console-border): the always-dark log surface, its toolbar and its dividers. It stays dark in both themes.
- **Console Ink** (console-foreground, console-text, console-muted): log text in three steps of emphasis, from command lines down to timestamps.

### Charts
- **Graphite Ramp** (chart-1 to chart-5): five steps of the page's own graphite on hue 260, darkest first. A chart's story series takes chart-1 and each context series steps lighter; chart-2 fills single-series bars, chart-3 carries a context line and the ledger's micro-bars, chart-4 their baseline. In dark theme the ramp reverses, lightest first (`oklch(0.9 0.006 260)` down to `oklch(0.29 0.008 260)`), so chart-1 is still the step with the most contrast against the page.
- **Delivery Palette** (viz-1 to viz-5, viz-ramp-1 to viz-ramp-4): the colour of delivery charts, where the series *are* the subject. viz-1…5 are a cool categorical set (blue, teal, amber, rose, violet) handed to the things a chart compares, such as models, in a fixed order by use and never cycled, so a model keeps its colour across charts, periods and projects; a sixth and later fold into Other in chart-3. viz-ramp-1…4 are one blue, strongest first, for a single story series (Delivered, the ledger's micro-bars, bars by agent) and for ordered kinds (tokens from output down to cache reads). Both sets pass the data-viz palette checks on each surface (colour-vision ΔE ≥ 11.7 between neighbours, normal vision ≥ 17.4); dark theme lifts them (`#5b88ec`, `#16998a`, `#c28027`, `#d6588f`, `#8f74e6`; ramp `#86a9f2` down to `#274a9c`). Amber sits under 3:1 on light, so these charts always carry a legend and a table twin.
- **Outcome Trio** (chart-positive, chart-neutral, chart-negative): a diverging green / grey / red for outcome charts only, such as accepted first time, after a rerun, closed unmerged. The trio was checked for colour-vision separation on both surfaces with the dataviz palette validator; dark theme lifts it to `#2f9a5a` / `#5f6570` / `#e06a6a`.

**Dark theme.** `.dark` on `<html>` swaps every token. The page becomes a graphite `oklch(0.18 0.008 260)`, text becomes `oklch(0.91 0.005 260)`, and the primary inverts to a pale `oklch(0.92 0.005 260)` with graphite text. Status badges keep their light values and are dimmed with `brightness(.85)`. Group tones, feedback surfaces and diffs have their own dark values in `index.css`.

### Named Rules
**The Signal-Only Color Rule.** Chrome is graphite on cool paper at chroma 0.014 or below. Saturated color appears only when it carries state: status, run signal, diff, presence, danger or focus. If it's colored, it means something.

**The One Dark Action Rule.** Control Ink fills exactly one primary action per view. Every other action is outline, secondary, ghost or link.

**The Pastel Pair Rule.** State is always a pastel surface with its own same-hue ink (`bg-status-running` + `text-status-running-foreground`). Never use a saturated fill with white text.

**The Series-Are-The-Subject Rule.** When a chart compares things people pick between, such as models, each gets a Delivery Palette hue in its fixed slot, the same hue on every chart of the page, with a legend carrying its total. A single story series takes viz-1 and its context stays graphite (chart-3); ordered kinds step down viz-ramp. Graphite alone is for context and for charts with nothing to tell apart.

**The Counted Legend Rule.** Outcome charts use the Outcome Trio and always carry a legend with each outcome's count. The neutral sits under 3:1 against the page, so the legend, not the fill, is what makes it readable.

**The One Dark Room Rule.** The log console is the only dark surface in light mode. Console tokens never leave logs, and logs never go light.

## Typography

**Body Font:** Geist Variable (with sans-serif)
**Label/Mono Font:** ui-monospace, SFMono-Regular, Menlo (for IDs, code and logs only)

**Character:** One neutral, slightly technical sans carries everything, at small sizes with fine weight steps (400, 450, 500, 550) instead of bold jumps. Hierarchy comes from size and tone, not from heavy weights or a second display face.

### Hierarchy
- **Display** (550, 1.625rem / 26px, line-height 1.3, −0.7px): the title of a record's detail page, such as a task or submission. It drops to 1.375rem when the shell is 600px or narrower.
- **Headline** (500, 1.3125rem / 21px, −0.5px): the body heading of a single-column view such as Settings or a list page (`ViewHeading`).
- **Title** (500, 0.875rem / 14px): the page name in the topbar, which is the page's `<h1>`, plus section titles and sheet titles.
- **Body UI** (400, 0.875rem / 14px): text you navigate or type in, such as sidebar items and inputs (`--text-ui`).
- **Body** (400, 0.8125rem / 13px, 1.5): default running text, descriptions and dialogs. Long descriptions are capped near 480–500px.
- **Label** (450, 0.75rem / 12px): buttons, table cells, form labels and toolbar controls.
- **Label Caps** (400, 0.6875rem / 11px, uppercase, 0.025em, Faint Slate): sidebar section labels only.
- **Micro** (450, 0.625rem / 10px, line-height 1.2): status badges and the status footer. 9px exists for the narrowest footer and is the floor.
- **Mono** (450, 0.75rem / 12px): task IDs, commit hashes, file paths and log lines.

### Named Rules
**The Rem-Only Rule.** Every text size is in rem. The person's text-size preference scales the root font size, so px text would stay small while everything else grows.

**The No-Hero Rule.** The largest type in the product is a record title at about 26–28px. Forge has no hero, marketing or display-scale headings. Emphasis comes from placement and tone.

## Layout

The shell is three fixed columns inside one `@container/shell`, divided by hairlines rather than gaps or tinted panels:
- **Icon rail:** 56px wide; 50px at 1270px or narrower, 58px at 1700px or wider, and hidden at 600px or narrower.
- **Workspace sidebar:** 224px wide; 205px at 1270px or narrower, 240px at 1700px or wider. At 1050px or narrower it becomes a sheet.
- **Main panel:** a topbar at least 58px tall, a scrolling page body with 22px side padding (18px, then 13px as the shell narrows), and a 34px status footer.

Regions respond to the **shell's** width through container queries at 1700, 1270, 1250, 1050, 900, 800, 760 and 600px (600 and 900 are the most common), never to viewport breakpoints. Portaled overlays sit outside the container and use viewport variants instead.

Spacing runs on the 4px Tailwind unit (`--spacing: 0.25rem`). Measured values are used where the design needs them (17px row insets, 21px between task groups, 26px under a view heading) rather than rounding everything to the scale. Lists are full width. Single-column views (settings, activity) sit on the page without card chrome.

**Canvas views** (the workflow builder) are the one place the page body is full-bleed: the canvas fills the main panel under the toolbar, and the workflow's details sit on a left Hairline at 22rem. They hold only the workflow itself (name, description, what's in it, what to fix, the canvas's keys); a step is never set up there. Its viewer can drag that edge (or use the arrow keys on it) between 18rem and 36rem, never past half the builder, and the width is remembered in the browser; a double click puts it back. At 900px or narrower the details leave the grid and float over the canvas as an 8px-radius card inset 12px, with the Float shadow, when asked for (the toolbar's Details, or the workflow's row in the sidebar). The details keep a 72px strip clear at their foot for the assistant launcher, which floats there.

**Density** is a preference. Compact tightens the spacing unit to 0.21875rem and list rows from 45px to 34px. Rows that should follow it use `h-(--forge-row-height)`.

### Named Rules
**The Shell-Width Rule.** Layout answers to `@container/shell`, not to the window. `md:` and `lg:` do not belong inside the shell.

**The Hairline Division Rule.** Regions (rail, sidebar, topbar, content, footer) are separated by 1px Hairline borders on one Cool Paper surface. They are never separated by alternating background fills or floating cards.

## Elevation & Depth

Forge is **flat until it floats**. At rest every surface lies on the same Cool Paper plane, and depth is conveyed by hairline borders, Pale Mist hover fills and pastel bands. A shadow means one thing: *this is above the page*. That covers menus, popovers, selects, toasts, sheets and dialogs. Two small exceptions are structural rather than atmospheric. The selected segment of a toggle or view tab lifts a hair (raised, tab), and empty-state illustrations use a faint paper shadow.

### Shadow Vocabulary
- **Float** (`box-shadow: 0 8px 35px oklch(0.2 0.01 260 / 12%)`): menus, popovers and floating panels. Sheets and dialogs use the equivalent large soft shadow.
- **Raised** (`box-shadow: 0 1px 3px oklch(0.2 0 0 / 5%)`): the selected segment in a toggle group, a selected workflow step, and the zoom controls and minimap that float over a canvas. A step being dragged takes Float.
- **Tab** (`box-shadow: 0 0 0 1px var(--border), 0 1px 2px oklch(0.2 0 0 / 4%)`): the active view tab in a toolbar.
- **Paper** (`box-shadow: 0 6px 15px oklch(0.3 0.02 260 / 4%)`): sheets of paper inside state illustrations only.

### Named Rules
**The Flat-Until-It-Floats Rule.** Resting surfaces carry no shadow. Outline buttons have no shadow, by design. If you reach for a shadow, the element must be floating above the page.

## Shapes

Corners are small and assigned by role, never by taste. They go from a 4px chip up to a 9px band, with a single jump to 16px for dialogs and sheets. Sheets are inset 12px from the viewport edges, so they read as floating cards rather than drawers bolted to the window. Circles are reserved for avatars, agent orbs, status symbols and connection dots. Borders are always 1px Hairline, except the status symbol's 1.8px dashed ring for queued work.

The one expressive form in the system is the **orb**. The workspace mark and the agent avatars are small circles filled with soft conic gradients (`bg-orb-workspace`, `orb-agent-0…4`). They are the only gradients in the product and identify who is who.

### Named Rules
**The Radius-by-Role Rule.** Use the role variable (`rounded-(--radius-control)`), not Tailwind's `rounded-md`:
- chip 4px: badges and chips.
- item 5px: menu items.
- soft 6px: small inset surfaces.
- control 7px: buttons, inputs, tabs and toggles.
- card 8px: cards, panels, the console and notices.
- band 9px: menus, popovers and status bands.
- dialog 16px: dialogs and sheets.

## Components

Every component is **quiet and exact**: small, precisely aligned and nearly invisible at rest. They sharpen on hover and focus and never shout. Build from the Forge composites (`@/components/forge`) first, then the tuned Base UI primitives (`@/components/ui`), and only then plain elements with tokens.

### Buttons
- **Shape:** gently squared corners (7px). The default height is 32px; `xs` is 24px, `sm` 28px and `lg` 36px. The type is 12px at weight 450, and icons are 16px.
- **Primary:** Control Ink with Paper on Ink text. There is one per view. Hover fades the fill to 80%.
- **Outline:** Cool Paper with a Hairline border and no shadow. Hover fills Pale Mist.
- **Secondary / Ghost:** Pale Mist fill, or no fill; both use Pale Mist on hover. Use these for toolbars and row actions.
- **Destructive:** a 10% red tint with red text, never a solid fill.
- **States:** colors transition over 150ms. Pressing nudges the button down 1px. Keyboard focus is a 3px ring at 30% opacity plus the global Focus Blue outline, and disabled buttons drop to 50% opacity. Saving shows an inline spinner with the button disabled.

### Status Badges and Chips
- **Status badge:** 19px tall with a 4px radius, 10px text at weight 450, and 7px side padding, with no border. The surface and text come from the matching status pair. The labels are Pending, Queued, Running, Running · Reviewing, Completed, Failed and Cancelled. `lg` is 23px with 12px text.
- **Chip:** the same shape, toned from the group tones, with an optional leading icon.
- **Status symbol:** a 14px glyph at the start of a band. It is a dashed ring for queued work, a ring with a dot for progress, a spinner for review and a filled check for done.

### Status Bands (signature)
A task group opens with a full-width pastel band, 39px tall with a 9px radius. It holds a status symbol, a label and a count in Slate Secondary, and ghost icon buttons that appear on hover. The rows below are 45px tall, or 34px in compact density. They have no dividers at rest. On hover a row gains a Hairline outline and a Pale Mist wash, rounded 8px at the ends. Group tones cascade: the band sets `--tone` / `--tone-foreground` and its children paint from them.

### Inputs / Fields
- **Style:** a 32px field with a 1px Hairline stroke on Cool Paper and a 7px radius. Text is 14px and placeholders are Slate Secondary.
- **Focus:** the border shifts to the ring color with a soft 3px ring at 30% opacity.
- **Error / Disabled:** errors use a Destructive Red border and a 20% ring, with the message in a `FieldError` below the field. Disabled fields drop to 50% opacity. Every dropdown is the custom `Select` on the shared menu surface; never use a native `<select>`.

### Menus, Popovers, Sheets and Dialogs
- **Menus / Popovers / Selects:** Cool Paper, Hairline border and a 9px radius, with 4px padding (12px in popovers), 5px item radius and a Float shadow.
- **Sheets:** inset 12px from the viewport with a 16px radius. They slide 2.5rem and fade over 200ms. Create and edit forms live here, with a footer holding the actions.
- **Dialogs:** a centered 16px-radius panel used for confirmation and short decisions only.

### Navigation
- **Sidebar items:** 36px or taller with a 7px radius. Text is 14px Nav Slate with a 17px icon. Hover and active states fill Pale Mist and turn the text Deep Graphite, and the active item also goes to medium weight (500). Trailing counts are 11px Faint Slate.
- **Section labels:** 11px uppercase Faint Slate with 0.025em letter-spacing.
- **Icon rail:** 56px of icon-only shortcuts, each with a tooltip and `aria-label`.
- **Topbar:** the breadcrumb ends in the page's `<h1>` (14px medium), with actions on the right, including the single primary action.
- **Mobile:** at 1050px or narrower the sidebar becomes a sheet opened from the topbar, and at 600px or narrower the rail hides.

### Log Console (signature)
Logs sit in an always-dark 8px-radius panel with a Console Night surface, a raised 44px toolbar and Console Border dividers. Text is monospace in three Console Ink steps, and it uses dark scrollbars. Success, warning and danger lines use the console's own muted green, gold and rose, never the light-theme status colors.

### Waiting Notice (signature)
Use this when work is waiting on a person or an agent. It is a warm-paper card with a Notice border and an 8px radius, a 40px round icon holder, an 18px heading at weight 550, and Notice text capped at about 500px. It is the only warm surface in the product, so it reads as "your turn" without using an alarm color.

### Charts (signature)
Charts are the shadcn chart primitive over recharts (`@/components/ui/chart`), painted only with chart tokens. They are quiet by construction:
- **Anatomy:** a hairline horizontal grid (Hairline at 50%), no vertical grid, no axis lines and no tick marks. Axis text is 11px Slate Secondary with an 8px margin. Every chart in one scope shares the same x-axis ticks, so panels line up date for date. One y-axis per chart; never a second axis. The plot keeps a little room at its right edge so the last date's label, centred on that edge, is never clipped.
- **Bars:** at most 24px wide, with a small rounded data end (2–3px) and square bases. Stacked segments are separated by a 1px edge in the page colour, not by gaps or borders.
- **Lines and areas:** 2px strokes on a monotone curve. The story series may carry an area wash at 8% opacity; context lines carry none.
- **Tooltip:** the Forge float surface: Cool Paper, Hairline border, 9px band radius, Float shadow, 12px text with values in medium-weight tabular figures, and 2px-rounded series marks (a 10px square dot, or a 4px-wide line).
- **Legend:** a row of 11px Slate Secondary labels, each with its 8px swatch (or a 12×2px line key for line series) and its total in Deep Graphite tabular figures.
- **Motion:** entrance animation is off. Charts change only when the data or scope does.
- **Access:** the drawn chart is hidden from assistive technology and carries a visually hidden table twin with a caption, one row per bucket.

### Chart Panels
Charts sit in hairline-divided panels, not cards: three columns in a row separated by 1px vertical Hairlines under a top Hairline, stacking with horizontal Hairlines at shell widths of 900px or narrower. Each panel reads top to bottom as a 12px medium title, a one-line answer in 12px Slate Secondary (two lines reserved so charts align), the chart (172px tall), its legend, and a facts list: Hairline-ruled rows with the label in Slate Secondary on the left and the value in medium tabular figures on the right.

### Portfolio Ledger (signature)
A comparison view is a ledger, not a wall of KPI tiles. It is the shared `DataTable` (title, description, Columns, View and Export like every other table) with the whole-scope row pinned on top in weight 550 and one row per peer below, in right-aligned tabular columns that sort, resize and hide.
- **Selection:** clicking a row, or Enter or Space on it, selects it. The selected row fills Pale Mist and sets its name to weight 550; the pinned whole-scope row carries no fill of its own, so the fill only ever marks the selection. The selection re-scopes everything below it, and the new scope fades in over 150ms so the change reads as one.
- **Frame:** wide columns scroll inside the table's own frame instead of widening the shell.
- **Micro-bars:** a row's delivered-per-week history is 12 columns 5px wide with 2px gaps, 22px tall, in viz-ramp-3 on a chart-4 baseline, always the last 12 weeks, and scaled to one peak shared down the whole ledger so rows compare at a glance.
- **Phones:** at shell widths of 600px or narrower the table gives way to a stacked list in a 9px-radius Hairline frame: a real `aria-pressed` name button and micro-bars on top, then the row's figures in a two-column grid of label and value.
- **Scope rollup:** at the site, the same ledger compares its organizations with the whole scope pinned on top as a total (weight 550, no fill, nothing to open). Here a child row is a door rather than a selection: it opens that child's own page, which repeats the composition one level down and keeps the period, and it ends in a quiet Faint Slate chevron that darkens on hover. Waiting on people is one Handoff Key holding the count in tabular figures, ringed when any has waited a day.

### Read-out Sentence
A data view may open with one sentence that states the period's result, set at headline size (21px, dropping to 17px at 600px or narrower) and capped near 60ch. The facts are in Deep Graphite at weight 550; the connecting words stay in Slate Secondary, so a skim of the dark words alone reads the result.

### Waiting Handoff Keys
Work waiting on a person shows as small warm cells carrying the task key: 19px tall, 4px chip radius, Notice surface and border, 10px monospace in the notice chip ink. A handoff that has waited a day or more gains a Notice Accent border and a 1.5px ring at 25%. Cells run oldest first; overflow collapses into a "+n" count, and a visually hidden line states the total and how many are a day old.

### Workflow Canvas (signature)
A workflow reads left to right like a whiteboard flowchart, drawn on React Flow repainted entirely from Forge tokens so it follows theme, contrast and reduced motion.
- **Canvas:** Cool Paper with a dot grid (16px gap, 1.1px dots in Faint Slate at 55%). No dark canvas, no colored category slabs.
- **Step card:** 15rem wide, 8px radius, 1px Hairline on Cool Paper. A header row holds the step's mark, its name (12px medium) and a detail line (11px Slate Secondary); an optional summary follows, as plain two-line text or, for expressions and code, a mono line on a Pale Mist chip. Selected: the border darkens to Deep Graphite at 45% with the Raised shadow.
- **Marks:** agent steps wear an agent orb (identity, not category); the entry point and End are Control Ink tiles, the start and finish of the flow; every other step is a 28px Hairline tile (6px radius) with a graphite glyph.
- **Approval steps:** the whole card sits on Waiting Notice paper with a Notice border, its tile inked in Notice Accent. It is the one "a person decides here" surface on the canvas.
- **Ways in and out:** 10px Cool Paper dots on a 1.5px graphite ring; the way in is on the header's left edge. A single way out sits on the header's right edge, so a straight run draws straight lines. Branching steps list each branch as a named 28px row under a Hairline, right-aligned, with its handle at the row's right edge; the fallback branch is in Faint Slate.
- **Connections:** 1.5px graphite (Slate Secondary at 62%) stepped at right angles with 10px corners; a connection that comes back is dashed (5 4). Resting connections carry no label, because the branch is already named on its row. Hover and selection turn the line Deep Graphite; a selected connection shows the branch name beside a remove button, and a library step held over a connection thickens it to 2px with an "Insert here" chip.
- **Issues:** a 14px mark at the card's top right, Destructive Red for errors and the amber tone ink for warnings, with the messages in a tooltip.
- **Step library:** lives in the builder's sidebar as grouped nav rows (Start, Agents & people, Actions, Logic, Finish) under the usual section labels, each wearing the same mark as its step; rows drag onto the canvas or add on click or Enter.
- **Workflow details:** the right rail described in Layout; a header with the workflow's 14px medium name and an 11px "click a step to set it up" line, then Hairline-divided sections: the name and description, In it (facts in a Hairline-ruled list), To fix (each step's issue opens that step's settings), Keys.
- **Step settings:** a click on a step (or Enter on a focused one) opens its settings in a dialog over the canvas, never in a side panel, so the step is set up where the eye already is. The dialog is as tall as the window less a 12px inset top and bottom, centered, `clamp(34rem, 34vw, 52rem)` wide (about a third of a wide screen) and never wider than the window less 12px each side, on the popover surface with the dialog radius, over a Black 20% backdrop (45% in dark) with no blur so the workflow stays legible behind it. It rises 8px as it fades in (150ms, ease-out). A header with the step's mark, 14px medium name, 11px kind and detail, and a close button; a scrolling body of Hairline-divided sections (the kind's summary with the step's issues, then its name and fields, then Data: how later steps read it and what it can read); a footer with Duplicate, Remove step, an 11px Faint Slate "Changes apply as you make them" and one Done primary. There is nothing to save. Escape, the backdrop, the close button and Done all close it; Escape first closes a field's completion list, and picking a completion never closes it. A ⌘, Ctrl or Shift click only selects, and a drag only moves.
- **Side by side:** a setting with a larger editor (a schema's fields) opens it as a second card of the same make, 47.5rem wide (shrinking to 28rem), 12px to the right of the settings; the pair is centred in the window together, never pushed to one side. The settings glide left to make room as the editor fades in from behind them (a 260ms view transition, ease-out; at once with motion off). Escape or a click on the backdrop closes the editor first, unsaved, then the settings. Below 1080px wide there is no room for two: the editor covers the settings card.
- **Controls:** zoom controls bottom-left in a 7px-radius Hairline bar, a Hairline minimap card bottom-right that hides at shell widths of 800px or narrower.
- **Navigation:** the wheel (or a pinch) zooms around the pointer; dragging empty canvas moves it, with a grab cursor; Shift-drag selects several steps.

### Expression Field
Every step setting that reads the run's data (a condition, a path, a template, code, an HTTP request's body) is an editor that knows what its step can read. It is the Input, extended; it never turns into a code editor's dark pane.
- **Field:** the Input's shape: a 1px Hairline on Cool Paper, 7px control radius, at least 32px tall, 10px side padding, placeholder in Slate Secondary. Focus moves the border to the ring color with the soft 3px ring at 30%; an error turns the border Destructive Red with a 20% ring. Warnings never redden the border. A transform's expression, arguments and prose templates start a set number of lines tall and grow; a one-line field folds a pasted newline into a space.
- **One language:** every expression is JSONata (conditions, values, lists, results, a transform, an HTTP body, each `{{ }}` in text), the language the Python runner evaluates; the builder never offers JavaScript.
- **Two faces:** expressions and arguments are 12px mono (line-height 1.6). Templates are prose, 14px sans like any Input, where each `{{ … }}` reads as a 13px mono chip on Pale Mist with the 4px chip radius. Typing `{{` closes itself and opens the completions.
- **Tokens:** the JSON view's colors, so a value reads the same in both places: strings in the green tone ink, numbers in the amber tone ink, true, false and null in the pink tone ink, operators, keywords and punctuation in Slate Secondary, names in Deep Graphite, comments in Faint Slate. The selection is Focus Blue at 22%.
- **Completion list:** the menu surface (popover Cool Paper, Hairline, 9px band radius, Float shadow, 4px padding), 15–28rem wide and at most 16rem tall, with no arrow. Items have the 5px item radius and 5px by 8px padding; the selected item sits on Pale Mist. A row reads: the step's mark, the label in 12px mono (matched letters in weight 600, never underlined), a note in 12px sans Slate Secondary that truncates, and the type right-aligned in 11px mono Slate Secondary. The marks repeat the canvas's StepGlyph at 18px (chip radius, 11px glyph at a 1.8 stroke): a 14px agent orb for an agent, the kind's glyph in paper on a Control Ink tile for the entry point or End, in Notice Accent on Waiting Notice paper for an approval, and in Slate Secondary on a Hairline tile for every other step. The list opens by itself only after `.`, `$` (JSONata's functions, each with its signature as the note), `{{`, the opening quote after `=` or `!=`, or a new argument; inside a filter (`items[…`) it offers the item's fields; Tab or Enter takes an item and Escape closes the list.
- **Info and hover:** the info panel beside the list (12px Slate Secondary, 18rem at most) and the tooltip that shows a path's type on hover (the type in 12px mono Deep Graphite over a 12px Slate Secondary line) float on the same surface, above the step dialog's scroll rather than clipped by it.
- **Diagnostics:** what doesn't fit gets a 1px wavy underline offset 3px, Destructive Red for an error and the amber tone ink for a warning. What's under the cursor isn't judged until the cursor leaves it. Under the field sit at most two messages, errors first, each a 13px icon and 12px text in the same color; any beyond that collapse into one 11px Slate Secondary line ("n more; hover the underlines to read them."). Messages take the place of the field's description, and go invisible (keeping their space) while the completion list is open so the list never covers them.
- **Voice:** plain second person. A message says what's wrong and the way out, and offers a did-you-mean when a name is close ("It declares kind, severity. Did you mean severity?").
- **Issues feed:** every field problem also joins the workflow's issues, prefixed with the setting's label ("Rule 4: …"), in the step's settings, the workflow details' To fix list and the topbar's count. An empty `{{ }}` is the field's own warning only, never a workflow issue.
- **Declared fields:** where a step declares its data (the start step's input, an agent's JSON output), both the same way, the step's settings show a 12px medium label with an outline xs button ("Declare fields", or "Edit fields" once there are some) over a list in a Hairline frame at the control radius. Rows are 6px by 10px and split by Hairlines: the name in 12px mono, "required" in 11px Faint Slate, the type right-aligned in 11px mono Slate Secondary. With nothing declared, one line of 12px Slate Secondary text says so. The fields are built in a second card beside the step's settings (see Step settings), holding the Events JSON Schema builder worded for what it declares (an input, an answer), with a footer of an 11px Faint Slate "Nothing changes until you save", Cancel and one Save fields primary. Nothing is declared inline.

### Empty, Loading and Error States
Every data view has all three:
- **Empty:** a small line illustration drawn in the product's own tokens, a one-line explanation and at most one action.
- **Loading:** skeletons shaped like the content.
- **Error:** an `ErrorCallout` with the message and a Retry action.

Background failures surface as toasts.

## Do's and Don'ts

### Do:
- **Do** build from Forge composites first, then the tuned primitives, then plain elements painted with tokens.
- **Do** spend Control Ink on exactly one primary action per view; everything else is outline, secondary, ghost or link.
- **Do** show state with the status pairs, group tones and run signals, through `StatusBadge`, `StatusSymbol`, `Chip` and `RunStateIcon`.
- **Do** write every size in rem, including one-offs such as `text-[0.8125rem]`, so the text-size preference reaches it.
- **Do** use the role radii (`rounded-(--radius-control)`, `rounded-(--radius-band)`) and keep borders at 1px Hairline.
- **Do** respond to `@container/shell` widths (1700 / 1270 / 1050 / 800 / 600px) inside the shell, and to viewport widths only in portaled overlays.
- **Do** keep motion to state changes (color transitions around 150ms, sheets around 200ms) and let the reduced-motion preference turn it all off.
- **Do** give every data view loading, empty and error states, and put a form-level failure in an `ErrorCallout` above the fields.
- **Do** build charts from `ChartContainer` with chart and viz tokens only: compared series (models) in their fixed Delivery Palette slots, a story series in viz-1 over graphite context, outcomes on the Outcome Trio with a counted legend, and a screen-reader table twin for every chart.
- **Do** compare peers in a ledger with a selectable total row on top, and let the selected row re-scope the detail below it.
- **Do** draw workflows as Hairline step cards on the dot-grid Cool Paper canvas, naming every branch on its own row with the handle at the row's right edge, and connecting steps with 1.5px graphite stepped lines (dashed when they come back).
- **Do** make every setting that reads run data an Expression Field: the Input's shape, mono for expressions and code, `{{ }}` chips in prose, JSON-view token colors, and completions and types on the menu surface.

### Don't:
- **Don't** drift toward a neon "AI" aesthetic: no dark-by-default screens, glowing gradients or purple-blue sparkle. The agent orbs are the only gradients.
- **Don't** build heavy enterprise dashboard chrome: no saturated blue, thick borders, card-boxed regions or big KPI tiles.
- **Don't** go playful consumer: no radius above 16px, bouncy or springy motion, oversized illustrations or bubbly type.
- **Don't** cosplay a terminal: monospace is for IDs, code and logs, and dark surfaces exist only in the log console.
- **Don't** use raw colors, hex values or Tailwind's default palette (`text-gray-500`, `bg-blue-600`); they break light/dark and the Forge look.
- **Don't** put a shadow on a resting surface; shadows mean "floating above the page".
- **Don't** restyle focus per component; the global 2px Focus Blue outline, offset 3px, wins on purpose.
- **Don't** use `NativeSelect` or a raw `<select>`; every dropdown is the custom `Select`.
- **Don't** put anything a person must read to act in Faint Slate (subtle); it is for hints, timestamps, placeholders and counts.
- **Don't** cycle or re-rank series colours, reach past five named series (fold the rest into Other), give a chart two y-axes, or animate a chart's entrance.
- **Don't** box charts in cards; panels are divided by Hairlines.
