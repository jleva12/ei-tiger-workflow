package python

import (
	"encoding/json"
	"os"
	"path/filepath"
	"slices"
	"testing"

	"ei-aitiger-codegraph/worker/internal/buildcontext/manifest"
	"ei-aitiger-codegraph/worker/internal/languages/python/packages"
	"ei-aitiger-codegraph/worker/internal/languages/python/project"
)

func TestConfiguration(t *testing.T) {
	for _, raw := range []string{`{}`, `{"version":"3.13","roots":["src"],"platform":"Darwin"}`} {
		cfg, err := configure(json.RawMessage(raw), "discover")
		if err != nil || cfg.Resolver == nil || cfg.SyntaxProfile.Language != "python" {
			t.Fatalf("%s: %+v %v", raw, cfg, err)
		}
	}
	for _, raw := range []string{`null`, `{"version":"2.7"}`, `{"platform":"host"}`, `{"max_heap_mib":1}`, `{"timeout_seconds":0}`, `{"roots":["../x"]}`, `{"typo":true}`, `{} {}`} {
		if _, err := configure(json.RawMessage(raw), "discover"); err == nil {
			t.Fatal("invalid configuration accepted", raw)
		}
	}
}

// Package installation is on with uv, off when disabled, and its settings
// are checked at startup rather than at the first run.
func TestInstallConfiguration(t *testing.T) {
	uv := filepath.Join(t.TempDir(), "uv")
	if err := os.WriteFile(uv, []byte("#!/bin/sh\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	cache := t.TempDir()
	installer := func(raw, mode string) (*packages.Config, error) {
		t.Helper()
		cfg, err := configure(json.RawMessage(raw), mode)
		if err != nil {
			return nil, err
		}
		provider, err := cfg.Discover("", manifest.Config{})
		if err != nil {
			t.Fatal(err)
		}
		return provider.(project.Provider).Config.Install, nil
	}
	got, err := installer(`{"install":{"uv_path":"`+uv+`","cache_dir":"`+cache+`","exclude":["private-sdk"]}}`, "discover")
	if err != nil || got == nil || got.UV != uv || got.CacheDir != cache || got.MaxBytes != 4096<<20 || !slices.Contains(got.Exclude, "torch") || !slices.Contains(got.Exclude, "private-sdk") {
		t.Fatalf("installer: %+v %v", got, err)
	}
	if got, err := installer(`{"install":{"enabled":false,"uv_path":"/missing/uv"}}`, "discover"); err != nil || got != nil {
		t.Fatalf("disabled: %+v %v", got, err)
	}
	if got, err := installer(`{"install":{"enabled":true,"uv_path":"`+uv+`"}}`, "syntax"); err == nil {
		t.Fatalf("installation without auto discovery accepted: %+v", got)
	}
	for _, raw := range []string{
		`{"install":{"enabled":true,"uv_path":"/missing/uv"}}`,
		`{"install":{"uv_path":"` + uv + `","index_url":"ftp://mirror"}}`,
		`{"install":{"uv_path":"` + uv + `","index_url":"--extra-index-url=https://x"}}`,
		`{"install":{"uv_path":"` + uv + `","exclude":["not a name"]}}`,
		`{"install":{"uv_path":"` + uv + `","max_mib":1}}`,
		`{"install":{"uv_path":"` + uv + `","timeout_seconds":100000}}`,
	} {
		if _, err := installer(raw, "discover"); err == nil {
			t.Fatalf("invalid installation settings accepted: %s", raw)
		}
	}
}
