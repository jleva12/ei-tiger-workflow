# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

**Primary: people who build and run their organization's business workflows.** They draw a process as steps on a canvas (an LLM agent step, a human approval, an HTTP call, a data transform, a branch or a loop), run it, and watch each run to see where work is and what is waiting on whom. They work in an organization's scope, which is where most day-to-day work in Forge happens.

**Also primary: the people a running workflow waits on.** Approvers decide the approval steps that reach them, often without having built the workflow.

**Supporting: the people who set up the structure around those organizations.** Site administrators manage organizations, users, roles and permissions. Organization administrators manage their members, the inbound events their organization accepts, and their workflows' approvals and runs. Their work exists so organizations can run their workflows; it is not the center of the product.

## Product Purpose

Forge is a business workflow engine: a place to design, run and supervise an organization's processes end to end. A workflow starts by hand or from another workflow, and its Start step can name the events from other systems that should start it. It then moves through its steps, calling LLM agents and outside services, transforming data, waiting, and pausing for a person wherever a decision belongs to one.

Success means an organization can put a real process into Forge and trust it, because every run is visible step by step, every decision a person made is recorded, and every run survives waits, failures and restarts.

**Built today:** the workflow builder (a canvas with a step library, step settings with JSONata expression fields and a live check of what to fix), workflow runs on the async worker with approvals, delays and nested workflows, inbound events with JSON Schema event types that a workflow's Start step can name, the site → organization hierarchy with role-based access, users and bearer tokens, and the assistant, which can draft, check, save and run workflows for the person it talks to. **Not built yet:** starting runs automatically when an event arrives (a received event is kept and checked, not yet dispatched to the workflows that name its type), and anything beyond the step catalog below. An agent step is a single LLM call with no tools, and there are no connections to third-party apps.

## Positioning

Forge runs governed business workflows, not a single assistant or a script runner. Agents are steps inside a workflow, working on the data the workflow gives them. A person decides wherever the workflow says one must, and every run and decision is audited. The claim Forge makes is the whole process, from the event that starts it to the last step, made visible and safe to run at organization scale.

## Operating Context

- **Hierarchy:** site → organization. Workflows, their runs, event types and members all belong to one organization. The site level is for administration.
- **Workspace scope:** a person works either in site administration (site administrators only) or in one of their organizations. The switcher at the top of the sidebar changes scope, and Home reopens the last one used.
- **Workflow steps:** Start, Agent (an LLM call, no tools), Approval, HTTP, Transform, Delay, Run workflow, If, Switch, Match, Loop, Merge and End. Every expression is JSONata, and a step can read what earlier steps produced.
- **Runs:** the admin API submits a run to the async worker, which runs it step by step as a tracked run: status, each step's output, failures and an audit trail. A run that waits (an approval, a long delay, another workflow) lets its worker go and picks up where it left off.
- **Event triggers:** each organization can turn on an inbound events endpoint and define event types, each with a JSON Schema. Another system posts an event to `/hooks/events/<endpoint>/<event type>` with the endpoint's token; the event is kept and checked against its schema. A workflow's Start step names the event types that trigger it, and the event type page and the builder edit that same link.
- **Forge assistant:** a Google ADK agent available from every page, served by the admin API on Gemini, or on the models of the shared model-provider configuration. It acts as the signed-in person, within their permissions: it drafts a workflow from what they describe and checks it the way the builder and the runner would, creates or saves it, runs workflows, follows their runs and decides approvals. It asks the person to confirm every change first. Until a model key is set, it explains how to set one.
- **Identity:** people sign in with a bearer token. Users are identified by first and last name, email and MS ID.

## Capabilities and Constraints

- **Hierarchy records:** organizations can be created, renamed and deleted. Every record carries who created and last changed it, and when.
- **Access as data (Casbin):**
  - Permission keys are `resource:action`, and either part may be `*`.
  - Roles are keyed `<level>:<name>`. A role is assigned in a scope (the site or an organization) and applies there and everywhere below it. The defaults are site administrator, organization administrator, organization member and organization viewer.
  - Roles, permissions and assignments change through the API without a deploy.
  - The API enforces every request. The console only decides which actions to offer.
- **Workflows as data:** each workflow is one `forge.workflow/v1` JSON document, checked against its JSON Schema on every save. Every save names the revision it was made from, so two people's changes never silently overwrite each other.
- **Approvals:** an approval step names who decides it: any member who may run workflows, or the organization's administrators. A run waits on it until someone decides or it expires.
- **Display preferences:** theme, density (comfortable/compact), text size, contrast (system/standard/more) and motion (system/reduce). They are saved in the browser.
- **Terminology:** organization, scope, site administrator, role, permission, member, workflow, step, run, approval, event, event type, endpoint, assistant, MS ID. Use these words as the product uses them.
- **Undecided:**
  - Whether agent steps gain tools, and which outside systems workflows may reach beyond HTTP steps.
  - Whether Forge is used only inside one company or offered more widely. No public or marketing surface exists.
  - Any formal accessibility conformance target.

## Brand Commitments

- **Name:** Forge (shown as "Forge Workspace" in the sidebar brand).
- **Design system (confirmed binding):** the Forge UI design system is the visual foundation. It comes from `jleva12/forge-ui`, is pinned in `apps/forge-web/skills-lock.json` and is documented for agents in `.claude/skills/forge-ui`. New work extends its composites, primitives, tokens and icon vocabulary; it does not replace them.
- **Voice (seen in shipped copy, not separately confirmed):**
  - Plain, second person, short sentences that say what happens and where.
  - Example: "None of your roles covers it. Ask an administrator for access."

## Evidence on Hand

- **The workflow format:** `apps/forge-admin-api/src/forge_admin/workflow.schema.json` and the step catalog, `workflow.catalog.json`, beside it.
- **Default roles and permissions:** seeded by the migrations in `apps/forge-admin-api/src/forge_admin/db/migrations/versions/`.
- **The shared Casbin model:** `apps/forge-admin-api/src/forge_admin/auth/casbin_model.conf`.
- **Missing assets:** the favicon is still Vite's default (`apps/forge-web/public/vite.svg`). No Forge logo file exists outside the design system's marks.
- **Absent, never to be invented:** customers, testimonials, usage metrics, benchmarks, pricing, case studies and deployment claims.

## Product Principles

1. **Organizations are where work happens; administration serves them.** Structure, roles and settings exist to put the right workflows and the right people in an organization.
2. **People decide where the workflow says they must.** Agents and steps carry out work. An approval stops a run until a person decides.
3. **Agents work inside the workflow.** An agent step sees only the data the workflow gives it and answers in the shape the workflow declares. There are no side doors.
4. **Make every run legible.** At any moment, show where a run is, what each step did, and what is waiting on whom.
5. **Access is enforced at the API; the interface only offers what you can do.** Nothing in the UI grants access, and nothing a person can't do is offered to them.

## Accessibility & Inclusion

No formal conformance standard has been chosen. The shipped display preferences are product commitments, so new UI must honor all of them: density, text size, stronger contrast, reduced motion, and theme. The shell's skip link must keep working as well.
