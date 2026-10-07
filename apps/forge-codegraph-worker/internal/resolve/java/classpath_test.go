package java

import (
	"archive/zip"
	"bytes"
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/semantic"
	"ei-aitiger-codegraph/worker/internal/buildcontext/manifest"
)

// A JAR whose zip64 extra field has a size recent JDKs refuse, as some old
// published JARs (aspectjweaver 1.8.x) have. javac cannot open it, and before
// the bridge left such entries out, every lookup of the context failed.
func TestUnreadableClasspathJarIsLeftOut(t *testing.T) {
	home := testJDK(t)
	src := []byte(`class A { String run() { return String.valueOf(1).trim(); } }`)
	var f *fixture
	f = newFixture(t, home, func(in *bc.Inventory, checks *[]bc.InputCheck) {
		in.Inputs = append(in.Inputs, bc.Input{ID: "old-jar", Kind: bc.InputJAR, Location: &bc.Location{Root: "checkout", Path: "lib/old.jar"}, SHA256: strings.Repeat("0", 64)})
		*checks = append(*checks, bc.InputCheck{InputID: "old-jar", Status: bc.Available, ObservedSHA256: strings.Repeat("0", 64)})
		in.Artifacts = append(in.Artifacts, bc.Artifact{ID: "old", Coordinates: bc.Coordinates{Group: "example", Name: "old", Version: "1", Extension: "jar"}, BinaryInputID: "old-jar"})
		in.SourceSets[0].Classpath = append(in.SourceSets[0].Classpath, bc.PathEntry{Kind: bc.EntryArtifact, RefID: "old"})
	})
	jar := filepath.Join(f.checkout, "lib", "old.jar")
	must(t, os.MkdirAll(filepath.Dir(jar), 0o700))
	var body bytes.Buffer
	w := zip.NewWriter(&body)
	entry, err := w.CreateHeader(&zip.FileHeader{Name: "META-INF/MANIFEST.MF", Method: zip.Store, Extra: []byte{0x01, 0x00, 0x04, 0x00, 0, 0, 0, 0}})
	must(t, err)
	_, err = entry.Write([]byte("Manifest-Version: 1.0\n"))
	must(t, err)
	must(t, w.Close())
	must(t, os.WriteFile(jar, body.Bytes(), 0o600))
	fp, err := manifest.Fingerprint(context.Background(), f.checkout, "lib/old.jar", bc.InputJAR, bc.DefaultLimits())
	must(t, err)
	for i := range f.build.Inventory.Inputs {
		if f.build.Inventory.Inputs[i].ID == "old-jar" {
			f.build.Inventory.Inputs[i].SHA256 = fp
		}
	}
	f.w.build = f.build
	f.addParsed("A", "main", "src/A.java", src, true)

	var logs bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&logs, nil)))
	defer slog.SetDefault(previous)
	r, err := New(Config{JavaHome: home, WorkDir: t.TempDir(), CacheDir: t.TempDir()})
	must(t, err)
	result, err := r.Resolve(context.Background(), f.req, f.w)
	must(t, err)
	if result.Unresolved != 0 || result.Resolved == 0 {
		for _, l := range f.lookups("A") {
			if l.Status != semantic.LookupResolved {
				t.Logf("%+v", l)
			}
		}
		t.Fatalf("result: %+v", result)
	}
	if !strings.Contains(logs.String(), "could not open were left out") || !strings.Contains(logs.String(), "old.jar") {
		t.Fatalf("skipped JAR not reported: %s", logs.String())
	}
}
