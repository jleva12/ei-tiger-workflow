# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

**Primary: people who build and run their organization's Google ADK workflows.** They draw a process as nodes on a canvas (an LLM agent or a team of them, a saved ADK workflow, a human approval or question, an HTTP call, a data transform, a branch or a loop), run it, and watch each run to see where work is and what is waiting on whom. They also design Google ADK chat agents. They work in an organization's scope, which is where most day-to-day work in Forge happens.

**Also primary: the people a running ADK workflow waits on.** Approvers decide the approvals that reach them, and people answer the questions a run asks, often without having built the ADK workflow.

**Supporting: the people who set up the structure around those organizations.** Site administrators manage organizations, users, roles and permissions. Organization administrators manage their members and their ADK workflows' approvals and runs. Their work exists so organizations can run their ADK workflows; it is not the center of the product.

## Product Purpose

Forge is where organizations build and run Google ADK workflows, and design Google ADK agents: a place to design, run and supervise an organization's processes end to end on [Google ADK](https://google.github.io/adk-docs/). An ADK workflow starts by hand, from the console. It then moves through its nodes on ADK's graph engine, calling LLM agents and outside services, transforming data, waiting, and pausing for a person wherever a decision or an answer belongs to one.

Success means an organization can put a real process into Forge and trust it, because every run is visible step by step, every decision a person made is recorded, and every run survives waits, failures and restarts.

**Built today:** the ADK workflow builder (a canvas with a node library, node settings with JSONata expression fields and a live check of what to fix), ADK workflow runs on the async worker with approvals, human input, delays and saved ADK workflows run inside others, the agent builder for Google ADK chat agents, the site → organization hierarchy with role-based access, users and bearer tokens, and the assistant, which explains access and helps administer organizations, people, roles and permissions. **Not built yet:** keeping agents anywhere but the browser, and running or chatting with them; starting runs other than by hand; tools for LLM agents inside an ADK workflow; the assistant reading or building ADK workflows; and connections to third-party apps.

## Positioning

Forge runs governed Google ADK workflows, not a single assistant or a script runner. LLM agents are nodes inside an ADK workflow, working on the data the workflow gives them. A person decides wherever the workflow says one must, and every run and decision is audited. The claim Forge makes is the whole process, from its start to the last step, made visible and safe to run at organization scale, on a framework Google maintains.

## Operating Context

- **Hierarchy:** site → organization. ADK workflows, their runs, agents and members all belong to one organization. The site level is for administration.
- **Workspace scope:** a person works either in site administration (site administrators only) or in one of their organizations. The switcher at the top of the sidebar changes scope, and Home reopens the last one used.
- **ADK workflow nodes:** Start; LLM agents, sequential, parallel and loop agents, and saved ADK workflows; human input and approvals; HTTP, Transform and Delay; If, Switch, Match, Loop, Merge and End. Every expression is JSONata, and a node can read what earlier steps produced.
- **Runs:** the admin API checks a run and submits it to the async worker, which runs it on Google ADK's graph engine, keeping its state in an ADK session, and tracks it as a background task: status, each step's output, failures and an audit trail. A run that waits (an approval, a person's answer, a long delay) lets its worker go and picks up where it left off.
- **Agents:** a builder for one Google ADK chat agent: its instructions and model, its tools and the agents it hands off to. Agents are kept in the browser for now; nothing runs them yet.
- **Forge assistant:** a Google ADK agent available from every page, served by the admin API on Gemini, or on the models of the shared model-provider configuration. It acts as the signed-in person, within their permissions: it explains their access and why something was refused, shows and changes who holds which role, and reads and edits organizations, roles, permissions and users. It asks the person to confirm every change first. Until a model key is set, it explains how to set one.
- **Identity:** people sign in with a bearer token. Users are identified by first and last name, email and MS ID.

## Capabilities and Constraints

- **Hierarchy records:** organizations can be created, renamed and deleted. Every record carries who created and last changed it, and when.
- **Access as data (Casbin):**
  - Permission keys are `resource:action`, and either part may be `*`.
  - Roles are keyed `<level>:<name>`. A role is assigned in a scope (the site or an organization) and applies there and everywhere below it. The defaults are site administrator, organization administrator, organization member and organization viewer.
  - Roles, permissions and assignments change through the API without a deploy.
  - The API enforces every request. The console only decides which actions to offer.
- **ADK workflows as data:** each ADK workflow is one `forge.agent/v1` JSON document, checked against its JSON Schema on every save. Every save names the revision it was made from, so two people's changes never silently overwrite each other.
- **Approvals:** an approval node names who decides it: any member who may run ADK workflows, or the organization's administrators. A run waits on it until someone decides or it expires. A human input node waits for an answer that fits what it asks.
- **Display preferences:** theme, density (comfortable/compact), text size, contrast (system/standard/more) and motion (system/reduce). They are saved in the browser.
- **Terminology:** organization, scope, site administrator, role, permission, member, ADK workflow, agent, node, step, run, approval, assistant, MS ID. Use these words as the product uses them.
- **Undecided:**
  - Which tools LLM agents get, and which outside systems ADK workflows may reach beyond HTTP nodes.
  - Whether Forge is used only inside one company or offered more widely. No public or marketing surface exists.
  - Any formal accessibility conformance target.

## Brand Commitments

- **Name:** Forge (shown as "Forge Workspace" in the sidebar brand).
- **Design system (confirmed binding):** the Forge UI design system is the visual foundation. It comes from `jleva12/forge-ui`, is pinned in `apps/forge-web/skills-lock.json` and is documented for agents in `.claude/skills/forge-ui`. New work extends its composites, primitives, tokens and icon vocabulary; it does not replace them.
- **Voice (seen in shipped copy, not separately confirmed):**
  - Plain, second person, short sentences that say what happens and where.
  - Example: "None of your roles covers it. Ask an administrator for access."

## Evidence on Hand

- **The ADK workflow format:** `apps/forge-admin-api/src/forge_admin/agent.schema.json`, generated from the web console's `src/lib/agents/schema.ts`.
- **Default roles and permissions:** seeded by the migrations in `apps/forge-admin-api/src/forge_admin/db/migrations/versions/`.
- **The shared Casbin model:** `apps/forge-admin-api/src/forge_admin/auth/casbin_model.conf`.
- **Missing assets:** the favicon is still Vite's default (`apps/forge-web/public/vite.svg`). No Forge logo file exists outside the design system's marks.
- **Absent, never to be invented:** customers, testimonials, usage metrics, benchmarks, pricing, case studies and deployment claims.

## Product Principles

1. **Organizations are where work happens; administration serves them.** Structure, roles and settings exist to put the right ADK workflows and the right people in an organization.
2. **People decide where the ADK workflow says they must.** Agents and nodes carry out work. An approval stops a run until a person decides.
3. **Agents work inside the ADK workflow.** An LLM node sees only the data the workflow gives it and answers in the shape the workflow declares. There are no side doors.
4. **Make every run legible.** At any moment, show where a run is, what each step did, and what is waiting on whom.
5. **Access is enforced at the API; the interface only offers what you can do.** Nothing in the UI grants access, and nothing a person can't do is offered to them.

## Accessibility & Inclusion

No formal conformance standard has been chosen. The shipped display preferences are product commitments, so new UI must honor all of them: density, text size, stronger contrast, reduced motion, and theme. The shell's skip link must keep working as well.
