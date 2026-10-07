package github

import (
	"context"
	"errors"
	"strings"
	"testing"
)

func TestCredentialsAreRefreshedForEachNetworkCommand(t *testing.T) {
	calls := 0
	service, err := New(Config{MirrorDir: t.TempDir(), TokenSource: func(ctx context.Context, owner, name string) (string, error) {
		calls++
		if owner != "team" || name != "repo" {
			t.Errorf("wrong repository %s/%s", owner, name)
		}
		return "scoped-token", nil
	}})
	if err != nil {
		t.Fatal(err)
	}
	service.run = func(ctx context.Context, dir string, env []string, args ...string) (string, error) {
		joined := strings.Join(env, "\n")
		expected := subcommand(args) == "fetch" || subcommand(args) == "clone"
		if strings.Contains(joined, "Authorization: Basic") != expected {
			t.Errorf("credential exposure for %v", args)
		}
		if strings.Contains(strings.Join(args, " "), "scoped-token") {
			t.Error("token in process arguments")
		}
		return "", nil
	}
	ctx := context.WithValue(context.Background(), repositoryKey{}, Request{Owner: "team", Name: "repo"})
	for _, args := range [][]string{{"fetch", "origin"}, {"fetch", "origin"}, {"rev-parse", "HEAD"}} {
		if _, err = service.git(ctx, "", args...); err != nil {
			t.Fatal(err)
		}
	}
	if calls != 2 {
		t.Fatalf("did not refresh credentials: %d", calls)
	}
	service.tokenSource = func(context.Context, string, string) (string, error) { return "", errors.New("installation revoked") }
	executed := false
	service.run = func(context.Context, string, []string, ...string) (string, error) { executed = true; return "", nil }
	if _, err = service.git(ctx, "", "fetch", "origin"); err == nil || executed {
		t.Fatal("fetched without authorization")
	}
}

// Without a token the service fetches anonymously, even when the process
// environment carries a token or Git credentials of its own.
func TestAnonymousFetchesClearAmbientCredentials(t *testing.T) {
	t.Setenv("GITHUB_TOKEN", "ambient-token")
	t.Setenv("GIT_CONFIG_COUNT", "1")
	t.Setenv("GIT_CONFIG_KEY_0", "http.https://github.com/.extraHeader")
	t.Setenv("GIT_CONFIG_VALUE_0", "Authorization: Basic ambient")
	for name, tokens := range map[string]func(context.Context, string, string) (string, error){
		"empty token": func(context.Context, string, string) (string, error) { return "", nil },
		"no source":   nil,
	} {
		service, err := New(Config{MirrorDir: t.TempDir(), TokenSource: tokens})
		if err != nil {
			t.Fatal(err)
		}
		calls := 0
		service.run = func(ctx context.Context, dir string, env []string, args ...string) (string, error) {
			calls++
			joined := strings.Join(env, "\n")
			if strings.Contains(joined, "Authorization:") || strings.Contains(joined, "ambient-token") {
				t.Fatalf("%s: anonymous Git command received credentials", name)
			}
			if !strings.Contains(joined, "GIT_TERMINAL_PROMPT=0") {
				t.Fatalf("%s: anonymous fetch can prompt for credentials", name)
			}
			return "", nil
		}
		ctx := context.WithValue(context.Background(), repositoryKey{}, Request{Owner: "team", Name: "repo"})
		for _, args := range [][]string{{"clone", "https://github.com/team/repo.git"}, {"fetch", "origin"}, {"ls-remote", "origin", "refs/heads/main"}} {
			if _, err := service.git(ctx, "", args...); err != nil {
				t.Fatal(err)
			}
		}
		if calls != 3 {
			t.Fatalf("%s: anonymous clone/fetch/ls-remote did not execute", name)
		}
	}
}
