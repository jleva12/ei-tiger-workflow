# Example workflows

Import one from the Workflows page: **Import** → **Choose a file** (or paste
its JSON). It gets a new ID, so it never replaces a workflow you have. Each is
checked by the worker's build (`packages/python/adk-workflows/tests/test_examples.py`)
and the builder's (`apps/forge-web/tests/agent-examples.test.mjs`).

## Repository change scout

`repo-change-scout.agent.json`: is a change needed in each of these
repositories to support a request, and if so, where?

```
Start {repos, prompt}
  └─ Each repository (loop, 4 at once)
       ├─ Clone the repository (Bash) ── error ─→ Couldn't clone it ─┐
       └─ success → Explore the repository (Pi agent, read-only)     │
                     → Its findings ──────────────────────────────────┤
  └─ done → Merge the findings → Done                    (back to the loop)
```

Run it with input like:

```json
{
  "repos": ["acme/billing", "acme/portal", "https://github.com/acme/web.git"],
  "prompt": "Let customers sign in with SAML SSO."
}
```

It hands back `{request, repositories, need_changes, no_changes, not_cloned,
findings}`: each finding is the repository's `needs_change`, a `summary` and
`changes` (`file`, `symbol`, `what`), with the commit it read.

**The worker** needs `HYBRID_ADK_WORKFLOWS__COMMANDS=local` (Bash steps),
`HYBRID_ADK_WORKFLOWS__PI_AGENT=local` with pi installed, and a
`HYBRID_ADK_WORKFLOWS__WORKSPACES` folder (each run clones into its own).

**Private repositories**: give the worker a GitHub PAT (read access to
contents) as `GITHUB_TOKEN`, and let commands read it:
`HYBRID_ADK_WORKFLOWS__TOOL_ENVIRONMENT='["GITHUB_TOKEN"]'`. The clone sends it
in a header for that command only, as actions/checkout does: it's never in the
workflow, the run's input, a URL, `.git/config` or the step's output, and the
Pi agents (which get a clean environment) never see it. Public repositories
need no token.

To change what's checked, edit the Pi agent's prompt or its answer's schema;
for more repositories at once, the loop's concurrency (each runs a pi process).
