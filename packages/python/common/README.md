# forge-common

Code the Forge Python apps and packages share (`import forge_common`). Each
subpackage brings its dependencies as an extra, so a codebase installs only
what it imports.

| Subpackage | Extra | What it holds |
|---|---|---|
| `forge_common.adk` | `adk` | `ForgeBaseToolset`: a Google ADK toolset whose tools never raise and never flood the model's context |
| `forge_common.model_provider` | `model-provider` | The shared `model_provider.yaml` files and their loader: the model providers and models the apps may use |
| `forge_common.adk.models` | `adk-models` | `ProviderModels`: an ADK model that runs each turn on one of `model_provider.yaml`'s models |
| `forge_common.adk.context_window` | `adk` | `ContextWindowPlugin`: each agent's context window kept in the session's state, and compacted in the middle of a turn, branch by branch |
| `forge_common.adk.ask_person` | `adk` | `ask_person`: ADK's `adk_request_input` tool for asking the person mid-turn, answered in the chat's form, and `repair_input_schemas` |
| `forge_common.adk.follow_ups` | `adk` | `FollowUpsPlugin`: follow-up questions after a reply, for the runs that ask for them, kept in the state for the chat to show |
| `forge_common.logging`, `forge_common.middleware` | `logging` | Structured logs for every logger (structlog), and `RequestContextMiddleware`: request ids and access lines |
| `forge_common.adk.memory` | `adk-memory` | `AtlasVectorMemoryService`: ADK memory in MongoDB Atlas, embedded by OpenAI and searched with Atlas Vector Search |
| `forge_common.templates` | `templates` | `TemplateEngine`: Jinja templates in a directory rendered to HTML with a dictionary, as a service apps inject |
| `forge_common.patterns` | `patterns` | Documents' regular expressions (JSON Schema's, JSONata's) on the `regex` engine, bounded in time and off the interpreter lock; `schema_errors`: a value held to a document's JSON Schema that way |
| `forge_common.mcp` | `mcp` | `bound_tool_results`: MCP tools' results held to their output schemas with `schema_errors`, rather than by the `mcp` client's own check |

A codebase depends on it with a path source, as on any shared package:

```toml
[project]
dependencies = ["forge-common[adk]"]

[tool.uv.sources]
forge-common = { path = "../../packages/python/common", editable = true }
```

## ForgeBaseToolset

A subclass of ADK's `BaseToolset`. A toolset lists its tools in
`get_raw_tools`; `get_tools` applies `tool_filter` and guards each one, so
every call answers in one of two shapes:

```jsonc
// The tool returned
{"status": "success", "payload": {"name": "forge", "stars": 3}}

// The tool raised
{"status": "failed", "reason": "KeyError: 'main'",
 "suggested_fixes": ["Call list_branches and retry with one of its names."]}

// The payload's JSON ran past max_result_chars
{"status": "success", "payload": [/* its first results */, "… [480 more items]"],
 "truncated": true,
 "complete_result_location": "/tmp/forge-tool-results/search_code-1a2b3c.json"}
```

```python
from google.adk.tools.function_tool import FunctionTool
from forge_common.adk import ForgeBaseToolset, ToolFailure


class GitToolset(ForgeBaseToolset):
    # Suggested for any failure nothing more specific covers.
    default_suggested_fixes = (
        "Check the arguments against the tool's parameters and call it again.",
    )

    async def get_raw_tools(self, readonly_context=None):
        return [FunctionTool(self.merge), FunctionTool(self.get_branch)]

    def merge(self, branch: str) -> dict:
        """Merges a branch."""
        if conflicts(branch):
            # A failure of the tool's own, with the fixes that suit it.
            raise ToolFailure(f"{branch} has conflicts", ["Rebase it, then merge again."])
        return {"merged": branch}

    def get_branch(self, name: str) -> dict:
        """A branch."""
        return branches[name]  # a KeyError fails the call too

    def suggest_fixes(self, tool, args, error):
        # Fixes for errors the tools can't anticipate, e.g. a client library's.
        if isinstance(error, KeyError):
            return [f"There's no branch {args['name']}: call list_branches first."]
        return super().suggest_fixes(tool, args, error)


agent = LlmAgent(model="gemini-3.5-flash", name="git", tools=[GitToolset(tool_name_prefix="git")])
```

- **Failures.** Any `Exception` a tool raises becomes a `failed` answer, logged
  with its traceback (a `ToolFailure` without one); cancellation still
  propagates. The fixes come from `suggest_fixes`, which by default returns a
  `ToolFailure`'s own fixes, else `default_suggested_fixes`; an override that
  returns None, or raises, gets `default_suggested_fixes` too. `suggest_fixes`
  sees the tool under its own name, without `tool_name_prefix`. Building the
  failed answer can't raise either: fixes that aren't a list of strings are
  dropped (a lone string counts as one fix). A tool that returns an error value
  instead of raising, like ADK's reply to a missing argument, is a `success`
  whose payload says so, unless `to_payload` fails it (below).
- **Failures a tool returns.** Override `to_payload(tool, result)` for tools
  that return their failures instead of raising them: raise `ToolFailure` to
  answer failed, or return the payload. It isn't called while a call waits on
  the user to confirm it or sign in.
- **Timeouts.** With `timeout_seconds` set (default None, no limit), a call
  that runs past it fails as a `ToolTimeout`, a `ToolFailure` whose fixes say
  to narrow the request and to check whether a change took effect before
  repeating it. Override `timeout_for(tool)` to give slow tools longer, or
  None; `suggest_fixes` can tell a timeout apart with
  `isinstance(error, ToolTimeout)`. The deadline stops an async tool at its
  next `await`. ADK runs a sync tool on the event loop, where nothing can stop
  it: its answer comes when it returns. A `TimeoutError` the tool raises
  itself is an ordinary failure.
- **Size.** A payload whose JSON runs past `max_result_chars` (default 20,000,
  about 5,000 tokens) is cut down to fit, keeping its shape: strings end early,
  lists and objects keep their first entries, and each cut says how much it
  left out. The whole payload is written to a new file in `result_dir`
  (default `$TMPDIR/forge-tool-results`, readable only by its owner): text as
  it is (`.txt`), anything else as indented JSON (`.json`). If the file can't
  be written, `complete_result_location` is null. `close()` deletes the files;
  a subclass that overrides it calls `super().close()`.
- **Media.** Images, audio or documents a tool returns as `types.Part`s move to
  the answer's `media` list, where ADK takes them to send as media parts; they
  don't count toward the limit.
- **ADK behaviour kept.** A guarded tool is a shallow copy of the tool with its
  own `run_async`, as ADK copies a tool to prefix it: its type, declaration,
  `process_llm_request`, confirmation and auth still work. A long-running tool
  that returns nothing is left for ADK to answer later. ADK's
  `on_tool_error_callback`s no longer see these tools' errors.
- **Listing.** A `get_raw_tools` that raises gives the agent no tools (logged)
  instead of ADK dropping the toolset. A subclass can't override `get_tools`:
  defining it raises `TypeError`.

The agent needs a tool that reads files to follow `complete_result_location`.

## Model provider configuration

`model_provider.yaml` names the model providers an app may use, their models
and how to reach them. There is one set of files, in
[`src/forge_common/model_provider/`](src/forge_common/model_provider), which
this loader reads for the admin API's assistant and for ADK workflows' LLM
nodes in the async worker. `model_provider.yaml` is the OAuth gateway example;
`model_provider.openai.yaml` uses OpenAI and Anthropic API keys (GPT-5.6,
GPT-5.2 and Claude Opus 5).

An app names its file by its repository path relative to the app's
directory, e.g.
`../../packages/python/common/src/forge_common/model_provider/model_provider.openai.yaml`
(`FORGE_ADMIN_MODEL_PROVIDER_CONFIG` in the admin API,
`HYBRID_ADK_WORKFLOWS__MODEL_PROVIDER_CONFIG` in the async worker). Their images
copy the files to the same path relative to `/app`, so the same path works
natively and in a container.

```python
from forge_common.model_provider import load_model_provider_config, supported_thinking_levels

config = load_model_provider_config(path)  # the shared model_provider.yaml by default
model = config.find("openai/gpt-5.2")  # or an id only one provider has
supported_thinking_levels(model.model)  # ["off", "minimal", "low", "medium", "high"]
```

- **Rules.** pi's model provider format: camelCase keys, with its defaults
  and checks, read as YAML 1.2 (`off: none` is a string key, a repeated key is
  an error). `${NAME}` is the environment variable NAME and `$${NAME}` a
  literal; a missing one fails, naming the variable and where it is. Errors
  never quote a value, which could be a credential, and keys and secrets are
  `SecretStr`.
- **Models** are named `provider/model` (`ResolvedModel.ref`), or by an id
  only one provider has. A `ResolvedModel` settles the model's API, base URL
  and headers (the model's merged over its provider's, whatever their case).
- **Thinking levels** follow pi: `off` only for a model that doesn't
  `reasoning`; `off` through `high` for one that does, and `xhigh` when its
  `thinkingLevelMap` maps it; a level mapped to null is left out.
  `clamp_thinking_level` picks the nearest a model offers, and
  `thinking_value` the provider's value for it.
- **OAuth2.** `OAuth2TokenSource` gets a provider's bearer token with a client
  credentials grant (a refresh token grant when there is one), reuses it until
  shortly before it expires, and never puts the token endpoint's response in
  an error.

### ProviderModels

`forge_common.adk.models.ProviderModels` is one ADK model an agent runs on.
It sends each turn to one of the configuration's models, so a conversation
can change model, and how long it thinks, from one turn to the next:

```python
from forge_common.adk.models import ProviderModels

models = ProviderModels(
    config, default="gpt-5.2", models=["openai/gpt-5.2", "google/gemini-3.5-flash"]
)


def choose(callback_context, llm_request):
    state = callback_context.state
    models.select(llm_request, state.get("model"), state.get("thinking_level"))


agent = LlmAgent(name="helper", model=models, before_model_callback=choose)
```

- **APIs.** `google-generative-ai` runs on ADK's `Gemini` with the provider's
  API key (OAuth2 isn't supported there; a base URL may end in its API
  version). `openai-responses`, `openai-completions` and `anthropic-messages`
  run on ADK's `LiteLlm` at the provider's base URL, with its key or its
  current OAuth2 token. Each API sends its key its own standard way, whatever
  `authHeader` says; pi's `compat` switches aren't used.
- **Thinking.** A level becomes Gemini's thinking config (a Gemini 3 level or
  a Gemini 2 budget; a `thinkingLevelMap` value may name either) or LiteLLM's
  `reasoning_effort` (the map's value, else the level). Without one, Gemini
  keeps its own level and shows its thoughts.
- **Tool calls.** ADK drops the call ids it made up itself unless the agent's
  model is one it knows pairs calls with results by id, which this isn't;
  before an OpenAI or Anthropic turn, calls without an id and their results
  get the same new one, so a conversation can move from Gemini to them.
- **Limits.** `maxTokens` caps each reply on the OpenAI and Anthropic APIs;
  Gemini keeps the model's own limit.

## Context windows

ADK compacts a session between invocations (an `App`'s
`EventsCompactionConfig`). Inside one it only checks the prompt the model last
reported, which leaves out what tools returned since, so a turn that reads many
files or pages can outgrow the model's window first.
`forge_common.adk.context_window.ContextWindowPlugin` goes on the `App`:

```python
from forge_common.adk.context_window import ContextWindowPlugin, context_of, windows_from

app = App(name="ca_x", root_agent=root, plugins=[ContextWindowPlugin(window=windows_from(models))])

context_of(session.state, "researcher")
# {"tokens": 152340, "limit": 272000, "threshold": 217600, "model": "openai/gpt-5.2", ...}
```

- **Counting.** Before each model call it measures the request (instructions,
  tool declarations, messages) and keeps it in the state under
  `forge:context:<agent>`: `tokens`, `limit` (the model's `window` less the
  `reply` it keeps, from `windows_from`), `threshold` (the `share` it
  compacts at), `auto`, `counted` (`estimate` or `provider`), `updated`,
  `compactions` and `last_compaction`. The window is the model the next
  request runs on: one just chosen in the state's `model` (`model_key`) counts
  from its first request. Characters per token start at 3.5 and follow what
  the provider reported; once the model answers, the count is the provider's.
  The console's composer shows it as a context meter beside the model picker.
- **Compacting.** At `threshold` (0.8 of what it may read), or when a tool or
  callback asks (`request_compaction(tool_context)`), it summarizes the older
  part of what the agent sees and rebuilds the request before it goes out.
  What started the turn and the agent's last step (`keep_steps`) stay word for
  word; before the turn and within it are summarized apart; a later
  compaction folds the earlier summaries in. `auto=False` only counts.
- **Branches.** Summaries are ADK compaction events only their agent reads:
  kept in the state by agent, branch and isolation scope
  (`summaries_of(state, agent, branch, scope)`) and added to that agent's view
  for each request, where ADK's contents step applies them. They're never
  appended to the session: ADK hides a compaction's whole time range from
  every agent that sees it, and an agent on no branch sees every branch, so a
  branch's summary would hide its siblings' events. Branches side by side
  (`fan.a`, `fan.b`), workflow steps and agents after them see the events as
  they were, and compact their own view when they need to.
- **Summaries** come from ADK's `LlmEventSummarizer` on the agent's own model,
  with a prompt that keeps findings, paths and what's left to do (`prompt`,
  or your own `summarizer`). Their model call isn't seen by other plugins.
- **Failures** never fail a turn: the request goes out as it was.

It relies on ADK 2.10's contents step (`google.adk.flows.llm_flows.contents`);
its tests check it against the ADK this workspace locks.

## Asking the person

`forge_common.adk.ask_person.ask_person` is ADK's own `adk_request_input`
tool with a description that teaches the model the answer schemas the Forge
chat turns into a form (one question per field of an object; pick one, pick
several, yes/no, numbers, dates, emails…). Calling it pauses the turn; the
person's answer comes back as the call's function response
(`{"result": …}`, in the schema's types) and carries the turn on.

```python
from forge_common.adk.ask_person import ask_person, repair_input_schemas

agent = LlmAgent(..., tools=[ask_person], after_model_callback=repair_input_schemas)
```

`repair_input_schemas` moves a `required` the model put among the
`properties` to beside them and drops what isn't a schema there: ADK checks
the answer against the schema, and fails on a property that isn't one.
`ASK_PERSON_INSTRUCTION` is a sentence for the agent's instruction. The
Forge assistant always has the tool; chat agents have it when their "Can ask
the person" setting is on; agents a workflow runs never do.

## Follow-up questions

`forge_common.adk.follow_ups.FollowUpsPlugin` goes on the `App`. A run asks
for follow-ups in its state delta (`forge:suggest_follow_ups`: `true`, or
`{"count": 1–5}`), as forge-ui's `useAdkAssistant({ suggestFollowUps: true })`
sends it. When the agent that took the turn ends it with a reply, the model
that wrote the reply (thinking off) writes a few short messages the person
might send next, from the conversation's last messages, and the run's last
event keeps them in the state as `forge:follow_ups` (`{"suggestions": [...]}`;
`follow_ups_of` reads it). The chat shows them above its composer.

```python
from forge_common.adk.follow_ups import FollowUpsPlugin

app = App(name="ca_x", root_agent=root, plugins=[FollowUpsPlugin()])
```

Each turn clears the last turn's first, so they always follow the last reply.
A turn that ends waiting on the person (a question, an approval, a sign-in)
gets none, and so does a run that didn't ask: an A2A call, another client, or
an agent run as a tool (`AgentTool`), which is given the caller's state but
not its request. `count`, `model`, `thinking_level`, `instruction` and
`timeout` change how they're written. Suggesting never fails a turn, and its
model call isn't recorded as usage (other plugins don't see it). The chat
agents' runtime (`build_app`) and the Forge assistant have the plugin; their
run-state checks allow the key.

## Logging

`configure_logging` routes every logger, the app's, uvicorn's and every
library's, through the stdlib root logger and renders it with structlog: one
JSON object per line for log collectors, or colored key=value lines on a
terminal. Values bound with `structlog.contextvars` are attached to every line,
libraries' included.

```python
from forge_common.logging import LoggingSettings, configure_logging, get_logger

configure_logging(settings.logging)  # a LoggingSettings field of the app's settings

log = get_logger(__name__)
log.info("order.created", order_id=order.id)
```

`logging.getLogger(__name__)` loggers keep working and render the same way.
An app nests `LoggingSettings` in its settings as `logging`, so it reads
`<PREFIX>LOGGING__LEVEL`, `__FORMAT` (`json` or `console`; unset, `console` on
a terminal and `json` otherwise), `__ACCESS_LOG`, `__ACCESS_LOG_EXCLUDE_PATHS`
and `__LEVELS` (per-logger levels for noisy libraries). Serve with uvicorn's
`log_config=None, access_log=False`, so its loggers come through here and its
access lines don't repeat the middleware's.

`RequestContextMiddleware`, an app's first middleware, gives each request an id
(the caller's `X-Request-ID` when it looks like one), binds it as `request_id`
to every line logged while the request runs, returns it in `X-Request-ID`, and
writes one `http.request` line per request with its status and duration. An
unhandled exception is logged once, with its traceback, and answers a JSON 500
carrying the request id.

## Templates

`TemplateEngine` renders the Jinja templates in a directory to HTML. An app
builds one at startup and hands it to the services that need it; each render
takes a template's path in the directory and a dictionary:

```python
from forge_common.templates import TemplateEngine

templates = TemplateEngine("templates")  # or [overrides, shared], or from_package("my_app")
html = templates.render("emails/welcome.html", {"user": {"name": "Ana"}, "items": items})
html = await templates.render_async("emails/welcome.html", data)  # in a thread, off the loop
```

Templates `{% extends %}` and `{% include %}` each other by their path; no name
reaches outside the directory. The defaults are safe for data from users: HTML
is autoescaped (`|safe` opts a trusted value out; `.txt` and `.md` templates
aren't escaped), a value the dictionary lacks raises `InvalidDataError` naming
it and the line that read it (`{{ x | default('') }}` marks optional ones), and
templates run in Jinja's immutable sandbox. Every failure is a
`TemplateRenderError`: `TemplateNotFoundError`, `InvalidTemplateError` (syntax)
or `InvalidDataError`, each with the `template` and `line` it happened at.
`filters=` and `template_globals=` add to every template; `environment` is the
Jinja environment for anything else.

Inject it as any shared service. In FastAPI, build it in the lifespan from a
`TemplateSettings` section of the app's settings (`<PREFIX>TEMPLATES__DIRECTORY`,
`__AUTO_RELOAD`, `__STRICT`, `__SANDBOXED`) and read it with a dependency:

```python
from typing import Annotated

from fastapi import Depends, Request
from forge_common.templates import TemplateEngine, TemplateSettings

class Settings(BaseSettings):
    templates: TemplateSettings = TemplateSettings()

# in the lifespan
app.state.templates = TemplateEngine.from_settings(settings.templates)

def get_templates(request: Request) -> TemplateEngine:
    return request.app.state.templates

Templates = Annotated[TemplateEngine, Depends(get_templates)]


class InvoiceMailer:
    def __init__(self, templates: TemplateEngine) -> None:
        self.templates = templates

    async def body(self, invoice: dict) -> str:
        return await self.templates.render_async("emails/invoice.html", {"invoice": invoice})
```

Tests inject one over a `jinja2.DictLoader({"emails/invoice.html": "..."})`
instead of a directory. A directory that doesn't exist fails at construction,
so a misconfigured path stops the app at startup rather than at the first render.

## Regular expressions from documents

Python's `re` matches in C holding the interpreter lock for as long as a
pattern backtracks: `^(a|aa)+$` on sixty `a`s and a `b` never finishes, and
nothing else of the process runs meanwhile, neither its event loop nor its
other threads. A pattern a document brings, a JSON Schema's or a JSONata
`/.../`, runs on the `regex` engine instead, through `forge_common.patterns`:

```python
from forge_common.patterns import compiled, schema_errors

pattern = compiled(r"^[A-Z]{2}\d{4}$", seconds=0.5)  # BoundedPattern: search, finditer, sub, split
pattern.search(text)  # lets go of the lock while it matches; PatternTimeout past half a second

problems = [error.message for error in schema_errors(document_schema, value)]
```

Each use of a `BoundedPattern` stops once it has taken its time, raising
`PatternTimeout` (a `ValueError`). Compiling isn't bounded so, and takes the
engine time in proportion to a pattern's repeats (`((a{1000}){1000}){1000}`
takes minutes, holding the lock): one that comes to more than
`MAX_COMPILED_SIZE` parts is refused before it's compiled, as is one that turns
on verbose mode or VERSION1. Both are `InvalidPattern`, as is one that doesn't
compile.

`schema_errors(schema, value)` holds a value to a JSON Schema that comes with a
document (draft 2020-12) with `SchemaValidator`: every keyword that matches a
pattern (`pattern`, `patternProperties`, and the properties
`additionalProperties` and `unevaluatedProperties` leave to it) matches on the
bounded engine, a subschema naming another draft (`$schema`) included, and all
of the value's patterns take `SCHEMA_SECONDS` (half a second) at most. Its
references (`$ref`, `$dynamicRef`) are looked up in the schema, and in the
drafts' meta-schemas, only: never fetched. A pattern that couldn't be run, or
a reference that can't be resolved, refuses the value, even under `not`,
`anyOf` or `if`. jsonschema's own validators match with `re`, fetch a
reference from wherever it points (with no timeout), raise for one they can't
resolve, and hand a subschema that names its draft to that draft's validator:
use them only for schemas of your own.

An MCP server's tools come with output schemas of its own (`outputSchema`),
and the `mcp` client checks every result against one with jsonschema
(`ClientSession.validate_tool_result`). `forge_common.mcp` (extra `mcp`) swaps
that check, for every session of the process, ADK's `McpToolset`'s among them,
for `schema_errors` on a worker thread, so the event loop goes on meanwhile. A
result that doesn't fit, can't be checked in time, or is missing answers the
call as the tool's error (`isError`), saying why, where the client raises. The
agent runtime and `forge_mcp_servers` swap it as they make an MCP toolset:

```python
from forge_common.mcp import bound_tool_results

bound_tool_results()  # before an MCP tool's first call; calling it again does nothing
```

## ADK memory in Atlas

`AtlasVectorMemoryService` is ADK's `InMemoryMemoryService` kept in MongoDB:
one document per event (or memory written directly) with text, holding its
content, author, time and embedding. A search embeds the query and runs
`$vectorSearch` over that app's and user's memories, nearest first.
`OpenAIEmbedder` makes the embeddings with OpenAI's embeddings API
(`text-embedding-3-small`, 1,536 dimensions, by default); `from_provider`
signs in as a model_provider.yaml provider does, with its base URL, headers
and key or OAuth2 token, so a gateway in front of OpenAI works too.

```python
from forge_common.adk.memory import AtlasVectorMemoryService, OpenAIEmbedder
from forge_common.model_provider import load_model_provider_config

memory = AtlasVectorMemoryService.from_uri(
    mongo_uri,
    database="forge_admin",
    embedder=OpenAIEmbedder.from_provider(load_model_provider_config(path), "openai"),
)
runner = Runner(app=app, session_service=sessions, memory_service=memory)
```

Like every ADK memory service, it keeps only what something adds: a callback
calling `ctx.add_session_to_memory()` after each turn (only new events are
embedded), a tool calling `tool_context.add_memory(...)`, or the app when a
conversation ends. Agents read it with ADK's `load_memory` or
`preload_memory` tools.

The collection (`adk_memories`) and its vector index are made on first use; a
new index takes a few seconds to build, and new memories take about a second
to become searchable. The index is made for the embedder's dimensions, so
changing them means a new collection or index. It works on any Atlas cluster
and on the `mongodb-atlas-local` image the Compose infrastructure runs.

Its tests run against that Mongo when `FORGE_TEST_MONGO_URI` (or
`FORGE_MONGO_URI`) is set, with a stand-in embedder, and skip otherwise.

## Checks

```sh
make common-check    # ruff, format check, mypy and pytest, in the workspace's .venv
make common-fmt
```
