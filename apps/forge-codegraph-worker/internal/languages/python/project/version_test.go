package project

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/worker/internal/languages/python/environment"
)

func TestDiscoverPythonVersion(t *testing.T) {
	for _, tc := range []struct {
		name, pin, pyright, requires, explicit, want, source, note string
	}{
		{name: "unconfigured", want: "3.12", source: "default"},
		{name: "patch pin", pin: "# managed by uv\n3.11.9\n", want: "3.11", source: ".python-version"},
		{name: "minor pin", pin: "3.13\r\n", want: "3.13", source: ".python-version"},
		{name: "pyright", pyright: "3.10", want: "3.10", source: "pyrightconfig.json pythonVersion"},
		{name: "pyright patch", pyright: "3.12.1", want: "3.12", source: "pyrightconfig.json pythonVersion"},
		{name: "agreeing pins", pin: "3.11.9", pyright: "3.11", requires: ">=3.11.8,<3.12", want: "3.11", source: ".python-version"},
		{name: "operator override", explicit: "3.13", pin: "3.14", pyright: "3.12", requires: ">=3.11", want: "3.13", source: "worker.version"},
		{name: "compatible default", requires: ">=3.10", want: "3.12", source: "requires-python:compatible-default"},
		{name: "newer minimum", requires: ">=3.13", want: "3.13", source: "requires-python:lowest-supported"},
		{name: "older maximum", requires: ">=3.10,<3.12", want: "3.10", source: "requires-python:lowest-supported"},
		{name: "minor exclusion", requires: ">=3.11,!=3.12.*", want: "3.11", source: "requires-python:lowest-supported"},
		{name: "patch floor", requires: ">=3.12.99,<3.13", want: "3.12", source: "requires-python:compatible-default"},
		// What the analysis cannot follow as written is noted, not fatal.
		{name: "conflicting pins", pin: "3.14", pyright: "3.12", want: "3.12", source: "pyrightconfig.json pythonVersion", note: "but .python-version is 3.14"},
		{name: "operator outside requirement", explicit: "3.12", requires: ">=3.14", want: "3.12", source: "worker.version", note: "outside requires-python"},
		{name: "pinned patch below floor", pin: "3.12.5", requires: ">=3.12.6", want: "3.12", source: ".python-version", note: "outside requires-python"},
		{name: "newer pin", pin: "3.14.1", want: "3.13", source: ".python-version", note: "analysed as Python 3.13"},
		{name: "older pin", pin: "3.8.18", want: "3.10", source: ".python-version", note: "analysed as Python 3.10"},
		{name: "newer requirement", requires: ">=3.14", want: "3.13", source: "requires-python:nearest-supported", note: "allows no Python the analysis supports"},
		{name: "older requirement", requires: ">=3.7,<3.10", want: "3.10", source: "requires-python:nearest-supported", note: "Python 3.10 is analysed"},
		{name: "contradictory range", requires: ">=3.13,<3.12", want: "3.12", source: "default", note: "allows no Python"},
		{name: "several pins", pin: "3.11\n3.12", want: "3.11", source: ".python-version", note: "the first, 3.11, is analysed"},
		{name: "host dependent", pin: "system", want: "3.12", source: "default", note: `"system" is not a numeric`},
		{name: "preview", pin: "3.14rc1\n3.12", want: "3.12", source: ".python-version", note: `"3.14rc1" is not a numeric`},
		{name: "unreadable requirement", requires: "^3.12", want: "3.12", source: "default", note: "could not be read"},
		{name: "unreadable pyright version", pyright: "latest", want: "3.12", source: "default", note: "is not a major.minor"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cfg := Config{Version: "3.12"}
			if tc.explicit != "" {
				cfg.Version, cfg.VersionExplicit = tc.explicit, true
			}
			options := map[string]any{}
			if tc.pyright != "" {
				options["pythonVersion"] = tc.pyright
			}
			got, notes, err := discoverVersion(cfg, options, "pyrightconfig.json pythonVersion", tc.pin, tc.requires)
			if err != nil || got.version != tc.want || got.source != tc.source {
				t.Fatalf("got %+v, %v; want %s from %s", got, err, tc.want, tc.source)
			}
			joined := strings.Join(notes, "\n")
			if (tc.note == "") != (len(notes) == 0) || !strings.Contains(joined, tc.note) {
				t.Fatalf("notes %q, want one containing %q", notes, tc.note)
			}
		})
	}
	// The worker's own configuration is still checked.
	if _, _, err := discoverVersion(Config{Version: "three", VersionExplicit: true}, nil, "", "", ""); !errors.Is(err, bc.ErrInvalidInput) {
		t.Fatalf("invalid worker version: %v", err)
	}
}

func TestStableReleaseSpecifiers(t *testing.T) {
	for _, tc := range []struct {
		spec, version string
		want          bool
	}{
		{"~=3.11", "3.12", true}, {"~=3.11.2", "3.12", false}, {"~=3.11.2", "3.11", true},
		{"==3.12.*", "3.12.8", true}, {"!=3.12.*", "3.12.8", false},
		{"==3.12", "3.12.1", false}, {"==3.12", "3.12.0", true},
		{">3.12", "3.12", true}, {">3.12.5,<3.12.6", "3.12", false},
		{">=3.12.5,<=3.12.6,!=3.12.5", "3.12", true},
		{"<=3.11", "3.11", true}, {"<3.11", "3.11", false},
		{"==3.*", "3.13", true}, {"~=3.12.0,!=3.12.*", "3.12", false},
	} {
		t.Run(tc.spec+"/"+tc.version, func(t *testing.T) {
			specs, err := parseSpecifiers(tc.spec)
			if err != nil {
				t.Fatal(err)
			}
			v, err := parseRelease(tc.version)
			if err != nil {
				t.Fatal(err)
			}
			if got := compatible(v, specs); got != tc.want {
				t.Fatalf("got %v", got)
			}
		})
	}
	for _, bad := range []string{"~=3", ">=3.12.*", "===3.12", ">=3.12,", "3.12", ">=3.14rc1", ">=3..12", ">=3.12 || <3.10"} {
		if _, err := parseSpecifiers(bad); err == nil {
			t.Fatalf("unsupported specifier accepted: %s", bad)
		}
	}
}

func TestVersionMetadataFlowsIntoInventory(t *testing.T) {
	root := t.TempDir()
	write := func(name, data string) {
		t.Helper()
		if err := os.WriteFile(filepath.Join(root, name), []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
	}
	write("pyproject.toml", "[project]\nrequires-python = '>=3.11,<3.14'\n")
	write(".python-version", "3.11.9\n")
	p := Provider{Config: Config{Version: "3.12", Platform: "Linux"}}
	req := bc.Request{Checkout: bc.Checkout{Path: root, RepositoryID: "repo", SnapshotID: "snapshot"}, Limits: bc.DefaultLimits()}
	first, err := p.Build(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	set := first.Inventory.SourceSets[0]
	s, err := environment.Decode(set.LanguageOptions, set.LanguageVersion)
	if err != nil {
		t.Fatal(err)
	}
	if set.LanguageVersion != "3.11" || s.VersionSource != ".python-version" || s.VersionRequest != "3.11.9" || s.RequiresPython != ">=3.11,<3.14" {
		t.Fatalf("discovery evidence lost: %+v", s)
	}
	write(".python-version", "3.13.2\n")
	second, err := p.Build(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if second.ID == first.ID || second.Inventory.SourceSets[0].LanguageVersion != "3.13" {
		t.Fatal("version change did not change inventory")
	}
	// A version outside the supported range is analysed at its nearest
	// end, and the build says so.
	write(".python-version", "3.14\n")
	third, err := p.Build(context.Background(), req)
	if err != nil || third.Inventory.SourceSets[0].LanguageVersion != "3.13" {
		t.Fatalf("3.14 repository: %v", err)
	}
	noted := false
	for _, gap := range third.Inventory.MissingInputs {
		noted = noted || (gap.Requested == bc.GapConfiguration && strings.Contains(gap.Reason, "analysed as Python 3.13"))
	}
	if !noted {
		t.Fatalf("no configuration gap: %+v", third.Inventory.MissingInputs)
	}
}

func TestPyrightJSONReplacesTOMLSettings(t *testing.T) {
	root := t.TempDir()
	for name, data := range map[string]string{
		"pyproject.toml":     "[tool.pyright]\npythonVersion = '3.10'\n",
		"pyrightconfig.json": "{}",
		".python-version":    "3.13.1",
	} {
		if err := os.WriteFile(filepath.Join(root, name), []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
	}
	got, err := (Provider{Config: Config{Version: "3.12", Platform: "Linux"}}).Build(context.Background(), bc.Request{Checkout: bc.Checkout{Path: root, RepositoryID: "repo", SnapshotID: "snapshot"}, Limits: bc.DefaultLimits()})
	if err != nil || got.Inventory.SourceSets[0].LanguageVersion != "3.13" {
		t.Fatalf("stale TOML target retained: %+v %v", got, err)
	}
}
