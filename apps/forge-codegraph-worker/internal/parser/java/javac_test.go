//go:build javac

package java

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"testing"
	"time"

	"ei-aitiger-codegraph/pkg/ir"
)

// Opt-in independent language oracle. Production extraction never runs javac.
// Every fixture declares its requirement and release; compilation uses no
// ambient classpath or annotation processors. Requires a JDK supporting 21.
func TestRequirementCompilerFixtures(t *testing.T) {
	compiler := os.Getenv("JAVAC")
	if compiler == "" {
		compiler = "javac"
	}
	path, err := exec.LookPath(compiler)
	if err != nil {
		t.Fatalf("compiler verification requires JAVAC or javac on PATH: %v", err)
	}
	data, err := os.ReadFile("testdata/requirements-javac.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Name, Requirement, Release string
		Files                      map[string]string
		Valid                      bool
	}
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	p := newTestParser(t)
	for _, tc := range cases {
		t.Run(tc.Requirement+"/"+tc.Name, func(t *testing.T) {
			dir := t.TempDir()
			paths := make([]string, 0, len(tc.Files))
			for path := range tc.Files {
				paths = append(paths, path)
			}
			sort.Strings(paths)
			args := []string{"--release", tc.Release, "-proc:none", "-encoding", "UTF-8", "-Xlint:-options", "-classpath", dir, "-sourcepath", dir, "-d", filepath.Join(dir, "classes")}
			for _, name := range paths {
				source := tc.Files[name]
				file := filepath.Join(dir, filepath.FromSlash(name))
				if err := os.MkdirAll(filepath.Dir(file), 0755); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(file, []byte(source), 0600); err != nil {
					t.Fatal(err)
				}
				args = append(args, file)
				in := inputFor(source)
				in.Source.Path = name
				in.Source.LanguageVersion = tc.Release
				f := parseTest(t, p, in)
				if tc.Valid {
					if f.Coverage.Status != ir.ExtractionComplete || f.LanguageValidation.Status != ir.LanguageNotChecked {
						t.Fatalf("valid fixture lost syntax or overclaimed validation: %+v / %+v", f.Coverage, f.LanguageValidation)
					}
				} else if f.Coverage.Status == ir.ExtractionComplete || f.LanguageValidation.Status != ir.LanguageInvalid {
					t.Fatal("known release violation was not reported")
				}
			}
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
			defer cancel()
			cmd := exec.CommandContext(ctx, path, args...)
			cmd.Dir = dir
			out, err := cmd.CombinedOutput()
			if ctx.Err() != nil {
				t.Fatal(ctx.Err())
			}
			if tc.Valid && err != nil {
				t.Fatalf("javac rejected required fixture: %v\n%s", err, out)
			}
			if !tc.Valid && err == nil {
				t.Fatal("javac accepted invalid release fixture")
			}
			if !tc.Valid {
				if _, ok := err.(*exec.ExitError); !ok {
					t.Fatalf("compiler failed to execute: %v", err)
				}
			}
		})
	}
}
