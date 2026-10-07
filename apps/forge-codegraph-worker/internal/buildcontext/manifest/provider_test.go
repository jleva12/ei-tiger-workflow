package manifest_test

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
	"ei-aitiger-codegraph/worker/internal/buildcontext/manifest"
	"ei-aitiger-codegraph/worker/internal/parser/java"
	"ei-aitiger-codegraph/worker/internal/repository/github"
)

type workspace struct {
	root, checkout, mount string
	provider              *manifest.Provider
	request               bc.Request
}

func TestNonJavaManifestProfile(t *testing.T) {
	w := setup(t)
	m := w.read(t)
	m.SchemaVersion = manifest.ManifestVersion
	m.Inventory = bc.Inventory{
		Inputs:  []bc.Input{{ID: "src", Kind: bc.InputSourceRoot, Location: &bc.Location{Root: "checkout", Path: "."}}},
		Modules: []bc.Module{{ID: "app", Name: "app", Directory: "."}},
		SourceSets: []bc.SourceSet{{ID: "main", ModuleID: "app", Name: "main", Kind: bc.SourceSetMain,
			Language: "python", LanguageVersion: "3.12", LanguageOptions: map[string]string{"mode": "strict"}, SourceRootIDs: []bc.InputID{"src"}}},
	}
	w.write(t, m)
	c, err := w.provider.Build(context.Background(), w.request)
	if err != nil {
		t.Fatal(err)
	}
	if len(c.Inventory.JDKs) != 0 || c.Inventory.SourceSets[0].LanguageOptions["mode"] != "strict" {
		t.Fatal("non-Java manifest profile changed", c)
	}
	language, version := c.Inventory.SourceSets[0].SyntaxLanguage()
	if language != "python" || version != "3.12" {
		t.Fatal(language, version)
	}
}

func setup(t *testing.T) workspace {
	t.Helper()
	root := t.TempDir()
	if err := os.CopyFS(root, os.DirFS("testdata")); err != nil {
		t.Fatal(err)
	}
	w := workspace{root: root, checkout: filepath.Join(root, "checkout"), mount: filepath.Join(root, "mount")}
	p, err := manifest.New(manifest.Config{Roots: map[string]string{"toolchain": w.mount, "artifacts": w.mount}})
	if err != nil {
		t.Fatal(err)
	}
	w.provider = p
	w.request = bc.Request{Checkout: bc.Checkout{Path: w.checkout, RepositoryID: "github.com/example/app", SnapshotID: "commit-1"}, Limits: bc.DefaultLimits()}
	return w
}
func (w workspace) read(t *testing.T) manifest.Manifest {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(w.checkout, manifest.DefaultPath))
	if err != nil {
		t.Fatal(err)
	}
	var m manifest.Manifest
	if err := json.Unmarshal(data, &m); err != nil {
		t.Fatal(err)
	}
	return m
}
func (w workspace) write(t *testing.T, m manifest.Manifest) {
	t.Helper()
	data, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	w.raw(t, string(data))
}
func (w workspace) raw(t *testing.T, data string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(w.checkout, manifest.DefaultPath), []byte(data), 0600); err != nil {
		t.Fatal(err)
	}
}
func (w workspace) build(t *testing.T) bc.BuildContext {
	t.Helper()
	c, err := w.provider.Build(context.Background(), w.request)
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
	return c
}
func checkByID(c bc.BuildContext, id bc.InputID) bc.InputCheck {
	for _, check := range c.Checks {
		if check.InputID == id {
			return check
		}
	}
	return bc.InputCheck{}
}

func TestManifestBuildAndIndependentPins(t *testing.T) {
	w := setup(t)
	c := w.build(t)
	if c.Status != bc.Complete || len(c.Diagnostics) != 0 || c.RepositoryID != w.request.Checkout.RepositoryID || c.SnapshotID != "commit-1" || c.Producer.Name != "explicit-manifest" {
		t.Fatalf("wrong envelope: %+v", c)
	}
	if len(c.Inventory.SourceSets) != 2 || len(c.Inventory.Artifacts) != 2 || len(c.Checks) != 8 {
		t.Fatal("inventory records lost")
	}
	main, test := c.Inventory.SourceSets[0], c.Inventory.SourceSets[1]
	if main.ID != "app-main" || main.TargetRelease != 21 || main.JDKID != "java21" || main.Classpath[0].RefID != "lib-v1" || len(main.Classpath) != 1 || main.ModulePath[0].RefID != "lib-v2" || test.Classpath[0].RefID != "app-main" || test.Classpath[1].RefID != "lib-v2" {
		t.Fatal("compilation environments were flattened")
	}
	if main.GeneratedRootIDs[0] != "generated" || main.OutputInputID != "classes" || c.Inventory.Artifacts[0].SourcesInputID != "sources-v1" || c.Inventory.Artifacts[0].Origin.SnapshotID != "library-commit-1" {
		t.Fatal("generated/output/source mappings lost")
	}
	// Independent known-answer pins in the checked-in manifest were produced
	// from the documented framing, not from this provider's Fingerprint helper.
	for id, want := range map[bc.InputID]string{"jdk": "eea04f3ab8b4615714f3a06d264c6fd582def9daae5add73fe952619e2d5a185", "generated": "0221d8f5d13ff021b9b937653da418afce773746ca7cdb5f60af4224bebfa131", "classes": "157ab878a073310dd88ef83d45258262bf62d39e6711b5866899a62c81fe0e51", "jar-v1": "4707dc3ffa79f4b7cea9c128b13b1dda191ec2e37164c88f3851704a56faa4b8"} {
		got := checkByID(c, id)
		if got.Status != bc.Available || got.ObservedSHA256 != want {
			t.Fatalf("%s: %+v", id, got)
		}
	}
	data, _ := json.Marshal(c)
	if strings.Contains(string(data), w.root) {
		t.Fatal("machine path leaked into persisted context")
	}
	var restored bc.BuildContext
	if err := json.Unmarshal(data, &restored); err != nil {
		t.Fatal(err)
	}
	if err := restored.Validate(); err != nil {
		t.Fatal(err)
	}
	other := setup(t).build(t)
	if !reflect.DeepEqual(c, other) {
		t.Fatal("mount location changed reproducible identity")
	}
}

func TestManifestCanonicalOrderingAndOwnership(t *testing.T) {
	w := setup(t)
	original := w.build(t)
	m := w.read(t)
	m.Inventory.Inputs[0], m.Inventory.Inputs[7] = m.Inventory.Inputs[7], m.Inventory.Inputs[0]
	m.Inventory.SourceSets[0], m.Inventory.SourceSets[1] = m.Inventory.SourceSets[1], m.Inventory.SourceSets[0]
	m.Inventory.Artifacts[0], m.Inventory.Artifacts[1] = m.Inventory.Artifacts[1], m.Inventory.Artifacts[0]
	data, _ := json.Marshal(m)
	w.raw(t, "\n"+string(data)+"\n")
	if !reflect.DeepEqual(original, w.build(t)) {
		t.Fatal("cosmetic manifest change changed context")
	}
	for i := range m.Inventory.SourceSets {
		if m.Inventory.SourceSets[i].ID == "app-test" {
			p := m.Inventory.SourceSets[i].Classpath
			p[1], p[2] = p[2], p[1]
		}
	}
	w.write(t, m)
	reordered := w.build(t)
	if reordered.ID == original.ID {
		t.Fatal("dependency order omitted from context identity")
	}
	reordered.Inventory.Inputs[0].Location.Path = "mutated"
	reordered.Checks[0].Status = bc.Missing
	if reflect.DeepEqual(reordered, w.build(t)) {
		t.Fatal("provider retained returned mutable state")
	}
	roots := map[string]string{"toolchain": w.mount, "artifacts": w.mount}
	p, err := manifest.New(manifest.Config{Roots: roots})
	if err != nil {
		t.Fatal(err)
	}
	roots["artifacts"] = "/does-not-exist"
	c, err := p.Build(context.Background(), w.request)
	if err != nil || c.Status != bc.Complete {
		t.Fatalf("provider aliased config map: %v", err)
	}
}

func TestMissingAndMismatchedInputsRemainExplicit(t *testing.T) {
	for _, kind := range []string{"missing_jar", "changed_jar", "missing_generated", "unknown_mount", "declared_unavailable", "declared_gap", "directory_instead_of_jar", "symlink"} {
		t.Run(kind, func(t *testing.T) {
			w := setup(t)
			m := w.read(t)
			id := bc.InputID("jar-v1")
			want := bc.Missing
			binary := filepath.Join(w.mount, "deps/lib-1.jar")
			switch kind {
			case "missing_jar":
				if err := os.Remove(binary); err != nil {
					t.Fatal(err)
				}
			case "changed_jar":
				if err := os.WriteFile(binary, []byte("other bytes"), 0600); err != nil {
					t.Fatal(err)
				}
				want = bc.DigestMismatch
			case "missing_generated":
				if err := os.RemoveAll(filepath.Join(w.checkout, "generated")); err != nil {
					t.Fatal(err)
				}
				id = "generated"
			case "unknown_mount":
				p, err := manifest.New(manifest.Config{})
				if err != nil {
					t.Fatal(err)
				}
				w.provider = p
			case "declared_unavailable":
				for i := range m.Inventory.Inputs {
					if m.Inventory.Inputs[i].ID == id {
						m.Inventory.Inputs[i].Location = nil
						m.Inventory.Inputs[i].UnavailableReason = "artifact not exported"
					}
				}
				w.write(t, m)
			case "declared_gap":
				m.Inventory.MissingInputs = []bc.MissingInput{{ID: "unknown-version", Requested: "example:optional:${version}", Reason: "version unavailable", ModuleID: "app", SourceSetID: "app-test"}}
				p := m.Inventory.SourceSets[1].Classpath
				m.Inventory.SourceSets[1].Classpath = append(append([]bc.PathEntry{p[0]}, bc.PathEntry{Kind: bc.EntryMissing, RefID: "unknown-version"}), p[1:]...)
				w.write(t, m)
			case "directory_instead_of_jar":
				os.Remove(binary)
				if err := os.Mkdir(binary, 0755); err != nil {
					t.Fatal(err)
				}
				want = bc.Unsupported
			case "symlink":
				os.Remove(binary)
				if err := os.Symlink("lib-2.jar", binary); err != nil {
					t.Fatal(err)
				}
				want = bc.Unsupported
			}
			c := w.build(t)
			if c.Status != bc.Incomplete || len(c.Diagnostics) == 0 {
				t.Fatal("missing evidence concealed")
			}
			if len(c.Inventory.Artifacts) != 2 || c.Inventory.SourceSets[0].Classpath[0].RefID != "lib-v1" {
				t.Fatal("missing dependency replaced or dropped")
			}
			if kind == "declared_gap" {
				if c.Inventory.SourceSets[1].Classpath[1].Kind != bc.EntryMissing || c.Diagnostics[0].GapID != "unknown-version" {
					t.Fatal("unresolved position or diagnostic lost")
				}
				return
			}
			check := checkByID(c, id)
			if check.Status != want {
				t.Fatalf("expected %s, got %+v", want, check)
			}
			if want == bc.DigestMismatch {
				sum := sha256.Sum256([]byte("other bytes"))
				if check.ObservedSHA256 != hex.EncodeToString(sum[:]) {
					t.Fatal("mismatch has wrong observed digest")
				}
				for _, x := range c.Inventory.Inputs {
					if x.ID == id && x.SHA256 == check.ObservedSHA256 {
						t.Fatal("bad artifact became its own expected pin")
					}
				}
			}
		})
	}
}

func TestRejectMalformedManifestAndIdentity(t *testing.T) {
	for _, tc := range []struct {
		name   string
		change func(manifest.Manifest) string
		want   error
	}{
		{"unknown_field", func(m manifest.Manifest) string {
			b, _ := json.Marshal(m)
			return strings.TrimSuffix(string(b), "}") + `,"surprise":true}`
		}, bc.ErrInvalidInput},
		{"duplicate_field", func(m manifest.Manifest) string {
			b, _ := json.Marshal(m)
			return strings.TrimSuffix(string(b), "}") + `,"schema_version":"1.0.0"}`
		}, bc.ErrInvalidInput},
		{"case_variant", func(m manifest.Manifest) string {
			b, _ := json.Marshal(m)
			return strings.Replace(string(b), `"inventory"`, `"Inventory"`, 1)
		}, bc.ErrInvalidInput},
		{"trailing_object", func(m manifest.Manifest) string { b, _ := json.Marshal(m); return string(b) + `{}` }, bc.ErrInvalidInput},
		{"schema", func(m manifest.Manifest) string { m.SchemaVersion = "2"; b, _ := json.Marshal(m); return string(b) }, bc.ErrUnsupportedVersion},
		{"repository", func(m manifest.Manifest) string {
			m.ExpectedRepositoryID = "other"
			b, _ := json.Marshal(m)
			return string(b)
		}, bc.ErrIdentityMismatch},
		{"snapshot", func(m manifest.Manifest) string {
			m.ExpectedSnapshotID = "other"
			b, _ := json.Marshal(m)
			return string(b)
		}, bc.ErrIdentityMismatch},
		{"path_escape", func(m manifest.Manifest) string {
			m.Inventory.Inputs[0].Location.Path = "../outside"
			b, _ := json.Marshal(m)
			return string(b)
		}, bc.ErrInvalidInput},
		{"invalid_utf8", func(m manifest.Manifest) string { return string([]byte{0xff}) }, bc.ErrInvalidInput},
	} {
		t.Run(tc.name, func(t *testing.T) {
			w := setup(t)
			w.raw(t, tc.change(w.read(t)))
			c, err := w.provider.Build(context.Background(), w.request)
			if !errors.Is(err, tc.want) || !reflect.DeepEqual(c, bc.BuildContext{}) {
				t.Fatalf("expected zero artifact and %v, got %v", tc.want, err)
			}
		})
	}
}

func TestBudgetsAndCancellation(t *testing.T) {
	w := setup(t)
	c := w.build(t)
	encoded, _ := json.Marshal(c)
	manifestBytes, _ := os.ReadFile(filepath.Join(w.checkout, manifest.DefaultPath))
	for _, tc := range []struct {
		name   string
		change func(*bc.Limits)
	}{
		{"input", func(l *bc.Limits) { l.MaxInputBytes = uint64(len(manifestBytes)) - 1 }},
		{"records", func(l *bc.Limits) { l.MaxRecords = c.RecordCount() - 1 }},
		{"files", func(l *bc.Limits) { l.MaxFiles = 1 }},
		{"hash_bytes", func(l *bc.Limits) { l.MaxHashBytes = 1 }},
		{"depth", func(l *bc.Limits) { l.MaxDepth = 2 }},
		{"output", func(l *bc.Limits) { l.MaxOutputBytes = uint64(len(encoded)) - 1 }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			request := w.request
			tc.change(&request.Limits)
			out, err := w.provider.Build(context.Background(), request)
			if !errors.Is(err, bc.ErrLimitExceeded) || !reflect.DeepEqual(out, bc.BuildContext{}) {
				t.Fatalf("budget not enforced: %v", err)
			}
		})
	}
	exact := w.request
	exact.Limits.MaxInputBytes = uint64(len(manifestBytes))
	exact.Limits.MaxRecords = c.RecordCount()
	exact.Limits.MaxOutputBytes = uint64(len(encoded))
	exact.Limits.MaxFiles = 22
	if _, err := w.provider.Build(context.Background(), exact); err != nil {
		t.Fatalf("exact limits rejected: %v", err)
	}
	exact.Limits.MaxFiles--
	if _, err := w.provider.Build(context.Background(), exact); !errors.Is(err, bc.ErrLimitExceeded) {
		t.Fatalf("filesystem count boundary lost: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	out, err := w.provider.Build(ctx, w.request)
	if !errors.Is(err, context.Canceled) || !reflect.DeepEqual(out, bc.BuildContext{}) {
		t.Fatal("canceled build returned artifact")
	}
	// Deterministically cancel inside the file hashing loop, not with a timing race.
	ctx, cancel = context.WithCancel(context.Background())
	defer cancel()
	counted := &cancelingContext{Context: ctx, cancel: cancel}
	counted.remaining.Store(6)
	file := filepath.Join(w.mount, "large.jar")
	if err := os.WriteFile(file, make([]byte, 256<<10), 0600); err != nil {
		t.Fatal(err)
	}
	if digest, err := manifest.Fingerprint(counted, w.mount, "large.jar", bc.InputJAR, bc.DefaultLimits()); digest != "" || !errors.Is(err, context.Canceled) {
		t.Fatalf("fingerprint cancellation failed: %q %v", digest, err)
	}
	if _, err := w.provider.Build(context.Background(), w.request); err != nil {
		t.Fatalf("provider not reusable after cancellation: %v", err)
	}
	missing := w.read(t)
	missing.Inventory.MissingInputs = []bc.MissingInput{{ID: "g1", Requested: "x", Reason: "missing"}, {ID: "g2", Requested: "y", Reason: "missing"}}
	w.write(t, missing)
	request := w.request
	request.Limits.MaxDiagnostics = 1
	if _, err := w.provider.Build(context.Background(), request); !errors.Is(err, bc.ErrLimitExceeded) {
		t.Fatalf("diagnostic limit ignored: %v", err)
	}
}

type cancelingContext struct {
	context.Context
	remaining atomic.Int32
	cancel    context.CancelFunc
}

func (c *cancelingContext) Err() error {
	if c.remaining.Add(-1) == 0 {
		c.cancel()
	}
	return c.Context.Err()
}

func TestConcurrentProviderAndParserHandoff(t *testing.T) {
	w := setup(t)
	want := w.build(t)
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			got, err := w.provider.Build(context.Background(), w.request)
			if err != nil || !reflect.DeepEqual(want, got) {
				t.Errorf("concurrent build: %v", err)
			}
		}()
	}
	wg.Wait()
	// The generic checkout contract accepts the existing GitHub service's
	// exact repository/SHA; no repository-specific import is needed by Provider.
	checkout := github.Checkout{Path: w.checkout, RepositoryID: w.request.Checkout.RepositoryID, CommitSHA: w.request.Checkout.SnapshotID}
	var provider bc.Provider = w.provider
	c, err := provider.Build(context.Background(), bc.Request{Checkout: bc.Checkout{Path: checkout.Path, RepositoryID: checkout.RepositoryID, SnapshotID: checkout.CommitSHA}, Limits: bc.DefaultLimits()})
	if err != nil {
		t.Fatal(err)
	}
	set := c.Inventory.SourceSets[0]
	data, err := os.ReadFile(filepath.Join(w.checkout, "src/main/java/demo/App.java"))
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(data)
	p, err := java.New()
	if err != nil {
		t.Fatal(err)
	}
	defer p.Close(context.Background())
	file, err := p.Parse(context.Background(), parser.Input{Source: ir.Source{FileID: "App.java", RepositoryID: c.RepositoryID, SnapshotID: c.SnapshotID, BuildContextID: string(c.ID), ModuleID: string(set.ModuleID), SourceSetID: string(set.ID), Path: "src/main/java/demo/App.java", ContentSHA256: hex.EncodeToString(sum[:]), SizeBytes: uint64(len(data)), Language: "java", LanguageVersion: strconv.Itoa(set.TargetRelease)}, Content: data, Options: parser.Options{EnablePreview: set.EnablePreview}, Limits: parser.DefaultLimits()})
	if err != nil || file.Source.BuildContextID != string(c.ID) || file.Source.SourceSetID != "app-main" || file.Coverage.Status != ir.ExtractionComplete {
		t.Fatalf("parser context handoff failed: %v", err)
	}
}

func TestProviderConfigurationBoundary(t *testing.T) {
	for _, config := range []manifest.Config{
		{ManifestPath: "../build.json"}, {ManifestPath: "/tmp/build.json"}, {ManifestPath: "."},
		{Roots: map[string]string{"checkout": "/tmp"}}, {Roots: map[string]string{"artifacts": "relative"}},
		{Roots: map[string]string{"invalid/name": "/tmp"}},
	} {
		if _, err := manifest.New(config); !errors.Is(err, bc.ErrInvalidInput) {
			t.Fatalf("invalid provider configuration accepted: %+v / %v", config, err)
		}
	}
	w := setup(t)
	for _, change := range []func(*bc.Request){
		func(r *bc.Request) { r.Checkout.Path = "relative" }, func(r *bc.Request) { r.Checkout.RepositoryID = "" },
		func(r *bc.Request) { r.Checkout.SnapshotID = "" }, func(r *bc.Request) { r.Limits.MaxHashBytes = 0 },
	} {
		r := w.request
		change(&r)
		out, err := w.provider.Build(context.Background(), r)
		if !errors.Is(err, bc.ErrInvalidInput) || !reflect.DeepEqual(out, bc.BuildContext{}) {
			t.Fatalf("invalid request accepted: %v", err)
		}
	}
	var uninitialized manifest.Provider
	if _, err := uninitialized.Build(context.Background(), w.request); !errors.Is(err, bc.ErrInvalidInput) {
		t.Fatalf("zero provider accepted: %v", err)
	}
}
