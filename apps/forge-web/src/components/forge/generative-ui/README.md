# Generative UI (Rich answers)

This module owns the UI trees an agent sends through its `present` tool.
The web console and desktop app both use this source. The assistant toolkit
registers `PresentUI` from `index.ts`; there is no separate desktop renderer.

## Where to work

| File | Responsibility |
| --- | --- |
| `components/<name>.tsx` | Forge's custom renderer for that component: Fact, Table, Chart, Steps, Choices, Markdown, Diagram, Map, DatePicker. |
| `components/<name>.ts` | A package-provided component's entry, forwarding its existing renderer. Input, Select, RadioGroup and CheckboxGroup add a visible label. |
| `components/lazy/` | Mermaid and Leaflet implementations, loaded only when a Diagram or Map is rendered. |
| `library.ts` | Explicit registration of every renderer. TypeScript checks it against the catalog's component names. |
| `catalog.ts` | Component groups, defaults, dependencies, actions, descriptions and schemas. Also produces the backend catalog. |
| `schemas/` | Forge-owned descriptions and Zod schemas; package components inherit their package schema. |
| `styles.css` | Rich-answer styling, scoped under `data-slot="rich-answer"` using the application's theme tokens. |
| `present-ui.tsx` | Accepted tool-call rendering and the live/answered fieldset. |
| `use-answer.ts`, `answer-context.ts` | Chat availability, selected replies, and sending a component's response as the next user message. |
| `actions.ts` | Converts an action's text and input into the user's reply. |
| `composition.ts` | Normalizes Card/Form compositions and prevents fields submitting before their enclosing form. |
| `messages.ts`, `choices-options.ts` | Reads tool verdicts and choices without loading UI components. |
| `result.ts` | Reads the base tree the server drew a call's team components (`org.…`) as (`treeOf`); a call is drawn from that tree, or from its arguments when it has none. |
| `adapters.ts`, `value.ts` | Shared renderer adapters and defensive value checks. |

Import `./catalog` from schema generation and configuration code. Keep it and
`schemas/` independent of the renderer registry, CSS and browser-only code.
The main `index.ts` is for rendering, so importing it pulls in the UI.

## Change an existing component

Open its named file in `components/` and, for appearance changes, the matching
selectors in `styles.css`. A package entry deliberately forwards to
`@assistant-ui/generative-ui/react` rather than duplicating upstream code.
Replace that entry with a custom renderer if it needs different behavior.
Keep the package's description and props schema when changing only rendering.

## Add a new component

1. Add `schemas/<name>.ts` with a description and a Zod `properties` object,
   following `schemas/choices.ts`. Register it in `CUSTOM_SCHEMAS` in
   `schemas/index.ts`. For a component already supplied by the package, its
   schema is inherited and this step is unnecessary.
2. Add its dialect name (for example, `"Weather"`) to the appropriate group in
   `RICH_ANSWER_GROUPS` in `catalog.ts`. Set dependencies in
   `RICH_ANSWER_REQUIRES` if needed. Add it to the defaults only if every new
   Rich answers node should offer it automatically.
3. Create `components/weather.tsx`. Export a React renderer that handles
   missing or malformed model props, uses the application's UI primitives and
   theme tokens, and supplies accessible labels. Follow `components/fact.tsx`
   for display content or `components/choices.tsx` for direct chat replies.
4. Import the renderer in `library.ts` and register it with
   `Weather: forgeComponent("Weather", RichWeather)`. This binds its schema and
   streaming metadata and removes internal `$...` props before rendering.
   Package-provided entries can be registered directly, as `Card` is.
5. If it uses `_action`, add its name to `RICH_ANSWER_ACTING` and preserve the
   package action contract. `forgeComponent` strips `$action`/`$dispatch`;
   action-aware adapters must explicitly handle those props, as DatePicker
   does. If it is a named form control, also add it to `FIELDS` in
   `composition.ts`, so the form submits its value once. Use `AnswerContext`
   for Forge interactions and respect `live` so historical answers cannot
   send another message.
6. Add styles under the rich-answer scope and regression coverage in
   `tests/assistant-rich-answers*.test.mjs`. Test rendering and any new action
   or form behavior, including answered/disabled state.
7. From `apps/forge-web`, run:

   ```sh
   npm run generate:agent-schema
   npm run check:agent-schema
   npm run typecheck
   npx eslint src/components/forge/generative-ui
   node --test tests/assistant-rich-answers*.test.mjs
   npm run build
   npm run build:desktop
   ```

   Commit the generated
   `packages/python/agent-runtime/src/forge_agent_runtime/present.catalog.json`
   with the source changes. The Python runtime builds the model's `present`
   schema from this file; it does not discover React components directly.
   A normal component using the existing tree/action contract needs no new
   Python rendering code. New tools, server-side data or validation semantics
   may need backend changes.
8. Restart/redeploy the runtime with that catalog, enable the component in the
   agent's Rich answers node, and save/publish the agent configuration as
   appropriate. Rebuild the web and desktop clients to ship the renderer.

Keep existing dialect names and props compatible with saved conversations.
The server's `shown: false` calls stay hidden; accepted compositions retain
their field values when they become disabled. Diagram and Map should remain
lazy so ordinary chats do not load Mermaid or Leaflet.
