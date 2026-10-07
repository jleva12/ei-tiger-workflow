# Shared service configuration

Module `ei-aitiger-codegraph/serviceconfig`. It holds the settings every
codegraph process shares and the code that turns them into a working store:

- `Config` with `Spanner` (database, scope, cursor signing key, emulator
  auto-provisioning), `Embedding` (provider, model, dimensions, concurrency),
  `Search` (query enhancement) and the `Queue` a worker claims ingestion
  jobs from (in the Forge admin API's MySQL).
- `Load`, the Viper loader: it derives every key from the configuration
  struct's `mapstructure` tags (or snake_case field names), then reads
  defaults, `codegraph.yaml` (or the file named by `CODEGRAPH_CONFIG_FILE`),
  the environment including a dotenv file selected by `CODEGRAPH_ENV_FILE`
  (`.env` by default), and optional command-line flags, in that order of
  precedence. Standard environment names are `CODEGRAPH_` plus the key path;
  `env` tags and `Options.Aliases` keep historical names. Unknown `CODEGRAPH_`
  keys in either file are rejected. Each application embeds `Config` with a
  `mapstructure:",squash"` tag and adds a section of its own: the worker its
  ingestion settings, the admin API, the query API and the MCP server theirs.
- `Open`, which provisions the emulator when asked, opens the store with the
  configured vector length, verifies it against the database, and builds the
  embedding provider.

`Embedding.VectorLength` is the dimension the database was created with;
every process that opens the same database must agree on it.

```sh
GOTOOLCHAIN=auto go test ./packages/go/code-graph/serviceconfig/...
```
