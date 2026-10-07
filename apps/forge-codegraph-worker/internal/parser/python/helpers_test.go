package python

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"testing"

	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
)

func inputFor(path, source string) parser.Input {
	content := []byte(source)
	digest := sha256.Sum256(content)
	return parser.Input{Source: ir.Source{FileID: "test-file", RepositoryID: "repo", SnapshotID: "snapshot", Path: path, ContentSHA256: hex.EncodeToString(digest[:]), SizeBytes: uint64(len(content)), Language: Language, LanguageVersion: "3.12"}, Content: content, Limits: parser.DefaultLimits()}
}

func fixture(t testing.TB, name string) string {
	t.Helper()
	source, err := os.ReadFile("testdata/" + name)
	if err != nil {
		t.Fatal(err)
	}
	return string(source)
}

func newTestParser(t testing.TB) *Parser {
	t.Helper()
	p, err := New()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := p.Close(context.Background()); err != nil {
			t.Error(err)
		}
	})
	return p
}

func parseTest(t testing.TB, p *Parser, input parser.Input) ir.SourceFile {
	t.Helper()
	file, err := p.Parse(context.Background(), input)
	if err != nil {
		t.Fatal(err)
	}
	if err := file.Validate(); err != nil {
		t.Fatal(err)
	}
	if file.Source != input.Source {
		t.Fatal("source metadata changed")
	}
	return file
}

func parseFixture(t testing.TB, name string) ir.SourceFile {
	t.Helper()
	return parseTest(t, newTestParser(t), inputFor("src/"+name, fixture(t, name)))
}

type declKey struct {
	kind ir.DeclarationKind
	name string
}

func declIndex(file ir.SourceFile) map[declKey][]ir.Declaration {
	out := map[declKey][]ir.Declaration{}
	for _, d := range file.Declarations {
		k := declKey{d.Kind, d.Name}
		out[k] = append(out[k], d)
	}
	return out
}

func one(t testing.TB, file ir.SourceFile, kind ir.DeclarationKind, name string) ir.Declaration {
	t.Helper()
	ds := declIndex(file)[declKey{kind, name}]
	if len(ds) != 1 {
		t.Fatalf("expected one %s %q, found %d", kind, name, len(ds))
	}
	return ds[0]
}

func callNames(file ir.SourceFile) map[string]int {
	out := map[string]int{}
	for _, c := range file.Calls {
		out[string(c.Kind)+":"+c.Name]++
	}
	return out
}

func typeByID(file ir.SourceFile, id ir.TypeRefID) ir.TypeRef {
	for _, t := range file.Types {
		if t.ID == id {
			return t
		}
	}
	return ir.TypeRef{}
}
