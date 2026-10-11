package workerapp

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestStaticTokenSource(t *testing.T) {
	// Every fetch, whichever repository it reads, uses the one token.
	source := staticTokenSource("github_pat_test")
	for _, repo := range [][2]string{{"acme", "widgets"}, {"other", "gadgets"}} {
		if got, err := source(context.Background(), repo[0], repo[1]); err != nil || got != "github_pat_test" {
			t.Fatalf("%s/%s: token %q, %v", repo[0], repo[1], got, err)
		}
	}
	// Without one, there is no source and Git reads anonymously.
	if staticTokenSource("") != nil {
		t.Fatal("an empty token made a source")
	}
}

func TestConfigValidation(t *testing.T) {
	valid := DefaultConfig()
	valid.Spanner.CursorSigningKey = strings.Repeat("k", 32)
	valid.JobsMySQLDSN = "forge_admin:secret@tcp(127.0.0.1:13326)/forge_admin"
	if err := valid.Validate(); err != nil {
		t.Fatal(err)
	}
	// Without a GitHub token the worker reads public repositories
	// anonymously; without a listener it serves no probes.
	for name, mutate := range map[string]func(*Config){
		"GitHub token": func(c *Config) { c.GitHubToken = "github_pat_" + strings.Repeat("t", 32) },
		"no listener":  func(c *Config) { c.HealthAddr = "" },
	} {
		c := valid
		mutate(&c)
		if err := c.Validate(); err != nil {
			t.Errorf("%s: %v", name, err)
		}
	}
	for name, mutate := range map[string]func(*Config){
		"bad listener":         func(c *Config) { c.HealthAddr = "8090" },
		"GitHub token newline": func(c *Config) { c.GitHubToken = "github_pat_x\n" },
		"GitHub token space":   func(c *Config) { c.GitHubToken = "token github_pat_x" },
		"no job queue":         func(c *Config) { c.JobsMySQLDSN = " " },
		"short cursor key":     func(c *Config) { c.Spanner.CursorSigningKey = "short" },
	} {
		c := valid
		mutate(&c)
		if err := c.Validate(); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

// An env file written for the worker's removed API still loads: its token
// and search settings are ignored.
func TestLoadConfigIgnoresTheRemovedAPISettings(t *testing.T) {
	dir := t.TempDir()
	t.Chdir(dir)
	env := filepath.Join(dir, "worker.env")
	settings := map[string]string{
		"CODEGRAPH_GRAPH_CURSOR_SIGNING_KEY": strings.Repeat("k", 32),
		"CODEGRAPH_JOBS_MYSQL_DSN":           "forge_admin:secret@tcp(127.0.0.1:13326)/forge_admin",
		"CODEGRAPH_ADMISSION_TOKEN":          strings.Repeat("a", 32),
		"CODEGRAPH_SEARCH_ENHANCE_QUERY":     "${FORGE_CODEGRAPH_SEARCH_ENHANCE_QUERY}",
	}
	var lines []string
	for name, value := range settings {
		lines = append(lines, name+"="+value)
		// Load exports what the file sets; restore the environment after.
		t.Setenv(name, "")
		if err := os.Unsetenv(name); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(env, []byte(strings.Join(lines, "\n")+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CODEGRAPH_ENV_FILE", env)
	t.Setenv("FORGE_ENV_COMMON_FILE", "")
	c, err := LoadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if c.JobsMySQLDSN != settings["CODEGRAPH_JOBS_MYSQL_DSN"] {
		t.Fatalf("job queue %q", c.JobsMySQLDSN)
	}
	if _, set := os.LookupEnv("CODEGRAPH_ADMISSION_TOKEN"); set {
		t.Fatal("the removed admission token reached the environment")
	}
}
