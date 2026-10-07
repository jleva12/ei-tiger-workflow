package serviceconfig

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/go-viper/mapstructure/v2"
	"github.com/spf13/pflag"
)

func hooks() []mapstructure.DecodeHookFunc { return []mapstructure.DecodeHookFunc{rootsHook} }

// worker mirrors how an application extends the shared configuration.
type worker struct {
	Config      `mapstructure:",squash"`
	WorkDir     string            `mapstructure:"work_dir"`
	Timeout     time.Duration     `mapstructure:"timeout"`
	Listen      string            `mapstructure:"listen" env:"LISTEN_ADDR"`
	Roots       map[string]string `mapstructure:"roots"`
	Parser      limits            `mapstructure:"parser"`
	Concurrency int               `mapstructure:"concurrency"`
	private     int
}

type limits struct {
	MaxSourceBytes uint64
	MaxIRRecords   uint64
}

func rootsHook(from, to reflect.Type, value any) (any, error) {
	if from.Kind() != reflect.String || to != reflect.TypeOf(map[string]string{}) {
		return value, nil
	}
	var roots map[string]string
	if err := json.Unmarshal([]byte(value.(string)), &roots); err != nil {
		return nil, err
	}
	return roots, nil
}

func defaults() worker {
	return worker{Config: Default(), WorkDir: ".work", Timeout: 2 * time.Hour, Roots: map[string]string{}, Parser: limits{MaxSourceBytes: 4096, MaxIRRecords: 100}, Concurrency: 1}
}

// isolate runs the test in an empty directory with no dotenv or config file
// in reach and no inherited CODEGRAPH_ variables.
func isolate(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	wd, _ := os.Getwd()
	if err := os.Chdir(dir); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chdir(wd) })
	for _, kv := range os.Environ() {
		name := strings.SplitN(kv, "=", 2)[0]
		if strings.HasPrefix(name, EnvPrefix+"_") || name == "LISTEN_ADDR" || name == "OPENAI_API_KEY" {
			t.Setenv(name, "")
			_ = os.Unsetenv(name)
		}
	}
	t.Setenv(EnvFileVar, "")
	_ = os.Unsetenv(EnvFileVar)
	t.Setenv(CommonFileVar, "")
	_ = os.Unsetenv(CommonFileVar)
	return dir
}

func TestKeys(t *testing.T) {
	c := defaults()
	keys, err := keysOf(&c, EnvPrefix, map[string][]string{"parser.max_source_bytes": {"CODEGRAPH_MAX_SOURCE_BYTES"}})
	if err != nil {
		t.Fatal(err)
	}
	byPath := map[string]key{}
	for _, k := range keys {
		byPath[k.Path] = k
	}
	cases := map[string][]string{
		"spanner.database":           {"CODEGRAPH_SPANNER_DATABASE"},
		"spanner.scope":              {"CODEGRAPH_SPANNER_SCOPE", "CODEGRAPH_SCOPE"},
		"spanner.cursor_signing_key": {"CODEGRAPH_SPANNER_CURSOR_SIGNING_KEY", "CODEGRAPH_GRAPH_CURSOR_SIGNING_KEY"},
		"embedding.api_key":          {"CODEGRAPH_EMBEDDING_API_KEY", "OPENAI_API_KEY"},
		"embedding.base_url":         {"CODEGRAPH_EMBEDDING_BASE_URL"},
		"log.level":                  {"CODEGRAPH_LOG_LEVEL"},
		"queue":                      {"CODEGRAPH_QUEUE"},
		"work_dir":                   {"CODEGRAPH_WORK_DIR"},
		"listen":                     {"CODEGRAPH_LISTEN", "LISTEN_ADDR"},
		"parser.max_source_bytes":    {"CODEGRAPH_PARSER_MAX_SOURCE_BYTES", "CODEGRAPH_MAX_SOURCE_BYTES"},
		"parser.max_ir_records":      {"CODEGRAPH_PARSER_MAX_IR_RECORDS"},
	}
	for path, env := range cases {
		k, ok := byPath[path]
		if !ok || !reflect.DeepEqual(k.Env, env) {
			t.Errorf("%s: got %+v, want env %v", path, k, env)
		}
	}
	if _, ok := byPath["private"]; ok {
		t.Error("unexported fields are not settings")
	}
	if byPath["timeout"].Default != 2*time.Hour || byPath["timeout"].Kind != reflect.Int64 {
		t.Errorf("duration default: %+v", byPath["timeout"])
	}
	for in, want := range map[string]string{"MaxIRRecords": "max_ir_records", "BaseURL": "base_url", "HealthAddr": "health_addr", "LeaseTTL": "lease_ttl", "APIKey": "api_key", "Workers": "workers", "MaxDepth2": "max_depth2"} {
		if got := snakeCase(in); got != want {
			t.Errorf("snakeCase(%s) = %s, want %s", in, got, want)
		}
	}
}

func TestLoadDefaultsAndEnvironment(t *testing.T) {
	isolate(t)
	c := defaults()
	if err := Load(&c, Options{Hooks: hooks()}); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(c, defaults()) {
		t.Fatalf("defaults survive an empty environment: %+v", c)
	}

	t.Setenv("CODEGRAPH_SPANNER_DATABASE", "projects/p/instances/i/databases/d")
	t.Setenv("CODEGRAPH_SCOPE", "legacy-scope")         // legacy alias
	t.Setenv("LISTEN_ADDR", "127.0.0.1:9000")           // env tag alias
	t.Setenv("CODEGRAPH_MAX_SOURCE_BYTES", "9")         // option alias into a nested untagged struct
	t.Setenv("CODEGRAPH_PARSER_MAX_IR_RECORDS", "7")    // standard name into a nested untagged struct
	t.Setenv("CODEGRAPH_TIMEOUT", "90s")                // duration
	t.Setenv("CODEGRAPH_ROOTS", `{"toolchain":"/opt"}`) // JSON through a hook
	t.Setenv("CODEGRAPH_LOG_LEVEL", "debug")
	t.Setenv("CODEGRAPH_EMBEDDING_DIMENSIONS", "1536")
	c = defaults()
	err := Load(&c, Options{Hooks: hooks(), Aliases: map[string][]string{"parser.max_source_bytes": {"CODEGRAPH_MAX_SOURCE_BYTES"}}})
	if err != nil {
		t.Fatal(err)
	}
	if c.Spanner.Database != "projects/p/instances/i/databases/d" || c.Spanner.Scope != "legacy-scope" || c.Listen != "127.0.0.1:9000" || c.Parser.MaxSourceBytes != 9 || c.Parser.MaxIRRecords != 7 ||
		c.Timeout != 90*time.Second || c.Roots["toolchain"] != "/opt" || c.Log.Level != "debug" || c.Embedding.Dimensions != 1536 || c.Concurrency != 1 {
		t.Fatalf("environment: %+v", c)
	}

	// The standard name wins over an alias when both are set.
	t.Setenv("CODEGRAPH_SPANNER_SCOPE", "standard-scope")
	c = defaults()
	if err = Load(&c, Options{Hooks: hooks()}); err != nil || c.Spanner.Scope != "standard-scope" {
		t.Fatalf("standard name precedence: %+v %v", c.Spanner, err)
	}

	// An empty value for a non-string setting is rejected; a wrong type too.
	t.Setenv("CODEGRAPH_CONCURRENCY", "")
	if err = Load(&c, Options{}); err == nil || !strings.Contains(err.Error(), "CODEGRAPH_CONCURRENCY") {
		t.Fatalf("empty non-string: %v", err)
	}
	t.Setenv("CODEGRAPH_CONCURRENCY", "many")
	if err = Load(&c, Options{}); err == nil {
		t.Fatal("wrong type must fail")
	}
}

func TestLoadDotenvAndConfigFile(t *testing.T) {
	dir := isolate(t)
	if err := os.WriteFile(filepath.Join(dir, ".env"), []byte("CODEGRAPH_WORK_DIR=/from/dotenv\nCODEGRAPH_QUEUE=file-queue\nOPENAI_API_KEY=sk-file\nUNRELATED=1\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CODEGRAPH_QUEUE", "process-queue")
	c := defaults()
	if err := Load(&c, Options{}); err != nil {
		t.Fatal(err)
	}
	// The process environment outranks the dotenv file, which outranks defaults.
	if c.WorkDir != "/from/dotenv" || c.Queue != "process-queue" || c.Embedding.APIKey != "sk-file" {
		t.Fatalf("dotenv precedence: %+v", c)
	}

	// A YAML configuration file sits below both.
	if err := os.WriteFile(filepath.Join(dir, "codegraph.yaml"), []byte("work_dir: /from/yaml\nconcurrency: 4\nspanner:\n  database: projects/y/instances/y/databases/y\nparser:\n  max_source_bytes: 2\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	c = defaults()
	if err := Load(&c, Options{}); err != nil {
		t.Fatal(err)
	}
	if c.WorkDir != "/from/dotenv" || c.Concurrency != 4 || c.Spanner.Database != "projects/y/instances/y/databases/y" || c.Parser.MaxSourceBytes != 2 {
		t.Fatalf("config file precedence: %+v", c)
	}

	// Unknown keys are errors in both files.
	if err := os.WriteFile(filepath.Join(dir, "codegraph.yaml"), []byte("work_dir: /x\nworkdirs: /y\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	c = defaults()
	if err := Load(&c, Options{}); err == nil || !strings.Contains(err.Error(), "workdirs") {
		t.Fatalf("unknown yaml key: %v", err)
	}
	if err := os.Remove(filepath.Join(dir, "codegraph.yaml")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, ".env"), []byte("CODEGRAPH_WORKDIR=/x\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	c = defaults()
	if err := Load(&c, Options{}); err == nil || !strings.Contains(err.Error(), "CODEGRAPH_WORKDIR") {
		t.Fatalf("unknown dotenv key: %v", err)
	}

	// An explicit env file must exist; an explicit config file is read from anywhere.
	t.Setenv(EnvFileVar, filepath.Join(dir, "missing.env"))
	if err := Load(&c, Options{}); err == nil {
		t.Fatal("explicit missing env file must fail")
	}
	// Dotenv values were exported into the process environment, as godotenv
	// does; clear them so the file below is not outranked.
	t.Setenv(EnvFileVar, "")
	for _, name := range []string{"CODEGRAPH_WORK_DIR", "CODEGRAPH_QUEUE", "OPENAI_API_KEY"} {
		t.Setenv(name, "")
		_ = os.Unsetenv(name)
	}
	// Earlier loads placed this value in the process environment. Disabling
	// dotenv loading does not remove it; clear it to test the config-file layer.
	if err := os.Unsetenv("CODEGRAPH_WORK_DIR"); err != nil {
		t.Fatal(err)
	}
	other := filepath.Join(dir, "elsewhere.toml")
	if err := os.WriteFile(other, []byte("work_dir = \"/from/toml\"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(ConfigFileVar, other)
	c = defaults()
	if err := Load(&c, Options{}); err != nil || c.WorkDir != "/from/toml" {
		t.Fatalf("explicit config file: %+v %v", c, err)
	}
}

func TestLoadFlags(t *testing.T) {
	isolate(t)
	t.Setenv("CODEGRAPH_WORK_DIR", "/from/env")
	flags := pflag.NewFlagSet("test", pflag.ContinueOnError)
	flags.String("work-dir", "/flag/default", "")
	flags.Int("concurrency", 3, "")
	if err := flags.Parse([]string{"--work-dir=/from/flag"}); err != nil {
		t.Fatal(err)
	}
	c := defaults()
	err := Load(&c, Options{Flags: flags, FlagKeys: map[string]string{"work-dir": "work_dir", "concurrency": "concurrency"}})
	if err != nil {
		t.Fatal(err)
	}
	// A flag given on the command line outranks the environment; an unset
	// flag leaves the configured default in place.
	if c.WorkDir != "/from/flag" || c.Concurrency != 1 {
		t.Fatalf("flags: %+v", c)
	}
	if err := Load(&c, Options{Flags: flags, FlagKeys: map[string]string{"nope": "work_dir"}}); err == nil {
		t.Fatal("binding an undefined flag must fail")
	}
}

func TestLoadRejectsNonStruct(t *testing.T) {
	isolate(t)
	var n int
	if err := Load(&n, Options{}); err == nil {
		t.Fatal("non-struct target")
	}
	if err := Load(defaults(), Options{}); err == nil {
		t.Fatal("non-pointer target")
	}
}

func TestLoadConfigAndValidate(t *testing.T) {
	isolate(t)
	t.Setenv("CODEGRAPH_GRAPH_CURSOR_SIGNING_KEY", strings.Repeat("k", 32))
	c, err := LoadConfig()
	if err != nil || c.Log.Level != "info" || c.Log.Format != "json" || c.Embedding.VectorLength() != 3072 {
		t.Fatalf("load: %+v %v", c, err)
	}
	t.Setenv("CODEGRAPH_LOG_FORMAT", "xml")
	if _, err = LoadConfig(); err == nil {
		t.Fatal("log format is validated")
	}
	t.Setenv("CODEGRAPH_LOG_FORMAT", "text")
	t.Setenv("CODEGRAPH_EMBEDDING_DIMENSIONS", "5000")
	if _, err = LoadConfig(); err == nil {
		t.Fatal("vector length is validated")
	}
}

// checkout lays out a repository root with .env.common and apps/forge-codegraph-worker/.env,
// and runs the test in apps/forge-codegraph-worker, as make worker does. Variables the files
// set are restored after the test.
func checkout(t *testing.T, common, env string) string {
	t.Helper()
	root := isolate(t)
	app := filepath.Join(root, "apps", "forge-codegraph-worker")
	if err := os.MkdirAll(app, 0o700); err != nil {
		t.Fatal(err)
	}
	if common != "" {
		if err := os.WriteFile(filepath.Join(root, ".env.common"), []byte(common), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(app, ".env"), []byte(env), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Chdir(app)
	for _, name := range []string{"FORGE_QUEUE", "FORGE_SHARED_SECRET", "FORGE_BASE", "FORGE_WORK_DIR", "CODEGRAPH_SCOPE"} {
		t.Setenv(name, "")
		_ = os.Unsetenv(name)
	}
	return root
}

func TestLoadDotenvCommonReferences(t *testing.T) {
	checkout(t,
		"FORGE_BASE=/shared\nFORGE_WORK_DIR=${FORGE_BASE}/work\nFORGE_QUEUE=common-queue\nFORGE_SHARED_SECRET=never-referenced\nOPENAI_API_KEY=sk-common\n",
		"CODEGRAPH_WORK_DIR=${FORGE_WORK_DIR}\nFORGE_QUEUE=app-queue\nCODEGRAPH_QUEUE=\"${FORGE_QUEUE}\"\nOPENAI_API_KEY=${OPENAI_API_KEY}\nCODEGRAPH_SCOPE='${FORGE_BASE}'\n")
	c := defaults()
	if err := Load(&c, Options{}); err != nil {
		t.Fatal(err)
	}
	// From .env.common (which may reference its own earlier lines), from an
	// earlier line of .env first, and single-quoted values literally.
	if c.WorkDir != "/shared/work" || c.Queue != "app-queue" || c.Embedding.APIKey != "sk-common" || c.Spanner.Scope != "${FORGE_BASE}" {
		t.Fatalf("references: %+v", c)
	}
	// Nothing in .env.common reaches the environment unless .env references it.
	for _, name := range []string{"FORGE_SHARED_SECRET", "FORGE_BASE", "FORGE_WORK_DIR"} {
		if _, set := os.LookupEnv(name); set {
			t.Fatalf("%s leaked from .env.common", name)
		}
	}
}

func TestLoadDotenvUndefinedReference(t *testing.T) {
	checkout(t, "OTHER=sensitive-value\n", "CODEGRAPH_WORK_DIR=${FORGE_WORK_DIR}\n")
	c := defaults()
	err := Load(&c, Options{})
	want := "CODEGRAPH_WORK_DIR in .env references ${FORGE_WORK_DIR}, which neither .env.common nor an earlier line defines (make env creates .env.common)"
	if err == nil || err.Error() != want || strings.Contains(err.Error(), "sensitive") {
		t.Fatalf("undefined reference: %v", err)
	}
	// Set by the process environment (as Compose does), the file's value is never used.
	t.Setenv("CODEGRAPH_WORK_DIR", "/from/environment")
	c = defaults()
	if err := Load(&c, Options{}); err != nil || c.WorkDir != "/from/environment" {
		t.Fatalf("environment wins over an undefined reference: %v %+v", err, c)
	}
	// .env.common's own references are checked too.
	checkout(t, "FORGE_WORK_DIR=${NOWHERE}/work\n", "CODEGRAPH_WORK_DIR=${FORGE_WORK_DIR}\n")
	if err := Load(&c, Options{}); err == nil || !strings.Contains(err.Error(), "FORGE_WORK_DIR in ../../.env.common references ${NOWHERE}") {
		t.Fatalf("undefined reference in .env.common: %v", err)
	}
}

func TestLoadDotenvCommonFileVariable(t *testing.T) {
	root := checkout(t, "FORGE_WORK_DIR=/default\n", "CODEGRAPH_WORK_DIR=${FORGE_WORK_DIR}\n")
	other := filepath.Join(root, "other.env")
	if err := os.WriteFile(other, []byte("FORGE_WORK_DIR=/other\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(CommonFileVar, other)
	c := defaults()
	if err := Load(&c, Options{}); err != nil || c.WorkDir != "/other" {
		t.Fatalf("another file: %v %+v", err, c)
	}
	_ = os.Unsetenv("CODEGRAPH_WORK_DIR")
	t.Setenv(CommonFileVar, "")
	if err := Load(&c, Options{}); err == nil || !strings.Contains(err.Error(), "FORGE_ENV_COMMON_FILE is empty") {
		t.Fatalf("disabled: %v", err)
	}
	t.Setenv(CommonFileVar, filepath.Join(root, "missing.env"))
	if err := Load(&c, Options{}); err == nil || !strings.Contains(err.Error(), "does not exist") {
		t.Fatalf("missing: %v", err)
	}
}
