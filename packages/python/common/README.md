# forge-common

Code the Forge Python apps and packages share (`import forge_common`). Each
subpackage brings its dependencies as an extra, so a codebase installs only
what it imports.

| Subpackage | Extra | What it holds |
|---|---|---|
| `forge_common.adk` | `adk` | `ForgeBaseToolset`: a Google ADK toolset whose tools never raise and never flood the model's context |
| `forge_common.model_provider` | `model-provider` | The shared `model_provider.yaml` files and their loader: the model providers and models the apps may use |
| `forge_common.adk.models` | `adk-models` | `ProviderModels`: an ADK model that runs each turn on one of `model_provider.yaml`'s models |
| `forge_common.logging`, `forge_common.middleware` | `logging` | Structured logs for every logger (structlog), and `RequestContextMiddleware`: request ids and access lines |

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

## Checks

```sh
make common-check    # ruff, format check, mypy and pytest, in this package's own .venv
make common-fmt
```
