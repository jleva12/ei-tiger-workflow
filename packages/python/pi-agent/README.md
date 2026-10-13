# forge-pi-agent

[Pi](https://pi.dev), the coding agent, driven over its
[JSON-RPC mode](https://pi.dev/docs/latest/rpc) from Python: one `pi --mode rpc`
process per `PiAgent`, working in the folder it's given, with the built-in tools
it's allowed, on a model from the shared `model_provider.yaml`.

```python
from forge_pi_agent import PiAgent, PiModel, PiOptions

model = PiModel(models.resolve("openai/gpt-5.2"), lambda: models.credential(resolved))
options = PiOptions(folder=repo, model=model, tools=("read", "grep", "find", "ls"), thinking="low")
async with PiAgent(options) as pi:
    result = await pi.prompt("Where is the login handled?", timeout=600)
print(result.text, result.tool_calls, result.usage)
```

- **Tools**: pi's built-in `read`, `grep`, `find`, `ls` (read-only, the default),
  `bash`, `edit` and `write`. `bash` runs any command as the process's user.
- **Models**: a `ResolvedModel` is written as pi's `models.json` (the format
  model_provider.yaml shares with pi); its credential is read from a private
  file for each request, so an OAuth2 token that's renewed is picked up.
- **Kept apart**: pi gets a private agent folder and `HOME` of its own, a clean
  environment, no extensions, skills, saved sessions or project trust (a
  repository's `.pi` never loads), and `AGENTS.md`/`CLAUDE.md` only when
  `context_files` is on.
- `prompt()` waits until pi has settled; `on_event` hears pi's events as they
  come; a model error pi gave up on, a timeout (pi is told to abort) or pi
  exiting raises `PiError`.

Needs `pi` on `PATH` (`npm install -g @earendil-works/pi-coding-agent`, Node 22.19+),
or `PiOptions.command`. Works with pi 0.81 and 1.x.

Tests: `make pi-agent-check` (a stand-in pi, `forge_pi_agent.fake_pi`, which other
packages' tests can start too: `PiOptions(command=fake_pi.COMMAND)`);
`FORGE_TEST_PI=1 OPENAI_API_KEY=… uv run pytest -m pi` runs the real one.
