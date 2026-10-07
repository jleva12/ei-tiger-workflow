package graphanalysis

import (
	"strings"
	"testing"

	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

const fixtureA = `package demo;

import java.util.List;

interface Handler {
    void handle(String s);
}

class Base {
    void run() {}
}

class A extends Base {
    @Override
    void run() {
        helper();
        Handler h = s -> helper();
    }

    void helper() {}

    void missing() {
        unknown();
    }
}
`

// fixtureMoved is fixtureA with helper and missing swapped: every canonical
// key survives, but the bytes and the declaration spans change.
const fixtureMoved = `package demo;

import java.util.List;

interface Handler {
    void handle(String s);
}

class Base {
    void run() {}
}

class A extends Base {
    @Override
    void run() {
        helper();
        Handler h = s -> helper();
    }

    void missing() {
        unknown();
    }

    void helper() {}
}
`

// method finds the method named name whose owner declaration is named owner.
func (f *fixture) method(in semantic.SourceInput, owner, name string) ir.Declaration {
	f.t.Helper()
	file := f.file(in)
	byID := map[ir.DeclarationID]ir.Declaration{}
	for _, d := range file.Declarations {
		byID[d.ID] = d
	}
	var found []ir.Declaration
	for _, d := range file.Declarations {
		if d.Kind == ir.DeclarationMethod && d.Name == name && byID[d.OwnerID].Name == owner {
			found = append(found, d)
		}
	}
	if len(found) != 1 {
		f.t.Fatalf("method %s.%s: %d declarations", owner, name, len(found))
	}
	return found[0]
}

func (f *fixture) methodEntity(in semantic.SourceInput, owner, name string) string {
	f.t.Helper()
	d, ok := f.identities(in).Declaration(f.method(in, owner, name).ID)
	if !ok {
		f.t.Fatalf("method %s.%s has no identity", owner, name)
	}
	return d.EntityID
}

func occurrenceIDs(ids semantic.FileIdentities) map[string]bool {
	out := map[string]bool{}
	for _, o := range ids.Occurrences {
		out[o.PersistentID] = true
	}
	return out
}

func TestMatcherAllocatesOnInitialRun(t *testing.T) {
	f := newFixture(t, "run-1")
	a := f.addFile("src/A.java", fixtureA)
	file := f.file(a)
	if len(file.Lambdas) != 1 {
		t.Fatalf("fixture has %d lambdas, want 1", len(file.Lambdas))
	}
	r := f.match(a)
	ids := f.identities(a)
	sites := len(occurrences(file))
	if r.Continued != 0 || r.Ambiguous != 0 || int(r.Allocated) != len(file.Declarations)+sites {
		t.Fatalf("initial run: %+v for %d declarations and %d sites", r, len(file.Declarations), sites)
	}
	if ids.Lineage != a.Lineage || ids.ContentSHA256 != a.Source.ContentSHA256 || ids.Path != a.Source.Path || len(ids.Declarations) != len(file.Declarations) || len(ids.Occurrences) != sites {
		t.Fatalf("identity map is incomplete: %+v", ids)
	}
	for _, d := range ids.Declarations {
		want := graph.ID("entity", "repo", "run-1", a.Lineage, string(d.DeclarationID))
		if d.EntityID != want || !graph.ValidID(d.EntityID) {
			t.Fatalf("declaration %s allocated %s, want %s", d.DeclarationID, d.EntityID, want)
		}
	}
	if d, _ := ids.Declaration(f.method(a, "A", "helper").ID); d.Key == nil || d.Key.CanonicalSignature != "demo.A.helper()" || d.Kind != ir.DeclarationMethod || d.Name != "helper" {
		t.Fatalf("helper identity lacks its key: %+v", d)
	}
	lambda := file.Lambdas[0].Occurrence
	found := false
	for _, o := range ids.Occurrences {
		if o.OccurrenceID == lambda.ID {
			found = true
			if o.EnclosingDeclarationID != lambda.EnclosingDeclarationID || o.EnclosingEntityID == "" || o.PersistentID != graph.ID("occurrence", "repo", "run-1", a.Lineage, string(lambda.ID)) {
				t.Fatalf("lambda occurrence identity: %+v", o)
			}
		}
	}
	if !found {
		t.Fatal("lambda site has no occurrence identity")
	}
}

func TestMatcherContinuesUnchangedFile(t *testing.T) {
	first := newFixture(t, "run-1")
	a := first.addFile("src/A.java", fixtureA)
	r1 := first.match(a)
	previous := first.identities(a)

	second := newFixture(t, "run-2")
	b := second.addFile("src/A.java", fixtureA)
	second.ws.previous[b.Lineage] = previous
	r2 := second.match(b)
	if r2.Allocated != 0 || r2.Ambiguous != 0 || r2.Continued != r1.Allocated {
		t.Fatalf("unchanged file: %+v, want everything continued (%d)", r2, r1.Allocated)
	}
	current := second.identities(b)
	for i, d := range current.Declarations {
		p := previous.Declarations[i]
		if d.DeclarationID != p.DeclarationID || d.EntityID != p.EntityID || d.Span != p.Span || d.OwnerID != p.OwnerID {
			t.Fatalf("declaration %d changed identity: %+v vs %+v", i, d, p)
		}
		if d.EntityID == graph.ID("entity", "repo", "run-2", b.Lineage, string(d.DeclarationID)) {
			t.Fatalf("declaration %s was reallocated in run-2", d.DeclarationID)
		}
	}
	for i, o := range current.Occurrences {
		p := previous.Occurrences[i]
		if o.OccurrenceID != p.OccurrenceID || o.PersistentID != p.PersistentID || o.EnclosingEntityID != p.EnclosingEntityID {
			t.Fatalf("occurrence %d changed identity: %+v vs %+v", i, o, p)
		}
	}
}

func TestMatcherAllocatesRenamedMethod(t *testing.T) {
	first := newFixture(t, "run-1")
	a := first.addFile("src/A.java", fixtureA)
	first.match(a)
	previous := first.identities(a)

	second := newFixture(t, "run-2")
	b := second.addFile("src/A.java", strings.ReplaceAll(fixtureA, "helper", "assist"))
	second.ws.previous[b.Lineage] = previous
	r := second.match(b)
	if r.Ambiguous != 0 {
		t.Fatalf("rename is not ambiguous: %+v", r)
	}
	for _, name := range []string{"Handler", "Base", "A"} {
		kind := ir.DeclarationClass
		if name == "Handler" {
			kind = ir.DeclarationInterface
		}
		if second.entity(b, kind, name) != first.entity(a, kind, name) {
			t.Fatalf("%s should continue through its unique key", name)
		}
	}
	for _, m := range [][2]string{{"Handler", "handle"}, {"Base", "run"}, {"A", "run"}, {"A", "missing"}} {
		if second.methodEntity(b, m[0], m[1]) != first.methodEntity(a, m[0], m[1]) {
			t.Fatalf("%s.%s should continue through its unique key", m[0], m[1])
		}
	}
	renamed := second.methodEntity(b, "A", "assist")
	if renamed == first.methodEntity(a, "A", "helper") {
		t.Fatal("renamed method kept the old identity")
	}
	if renamed != graph.ID("entity", "repo", "run-2", b.Lineage, string(second.method(b, "A", "assist").ID)) {
		t.Fatal("renamed method was not freshly allocated")
	}
	// After an edit, sites continue only inside continued entities, by kind,
	// name and order; sites of the renamed (freshly allocated) method do not.
	before := occurrenceIDs(previous)
	now := second.identities(b)
	continuedSites, freshSites := 0, 0
	for _, o := range now.Occurrences {
		if before[o.PersistentID] {
			continuedSites++
			if o.EnclosingEntityID == renamed {
				t.Fatalf("site %s of the renamed method continued an old identity", o.OccurrenceID)
			}
		} else {
			freshSites++
		}
	}
	if continuedSites == 0 || freshSites == 0 {
		t.Fatalf("expected both continued and fresh sites, got %d continued and %d fresh", continuedSites, freshSites)
	}
	// Keyless declarations (parameters, locals) of continued owners continue
	// by kind, name and order; those of the renamed method are fresh.
	prevEntities := map[string]bool{}
	for _, d := range previous.Declarations {
		prevEntities[d.EntityID] = true
	}
	continuedDecls := 0
	for _, d := range now.Declarations {
		if prevEntities[d.EntityID] {
			continuedDecls++
			if d.OwnerID != "" {
				if owner, _ := now.Declaration(d.OwnerID); owner.EntityID == renamed {
					t.Fatalf("declaration %s inside the renamed method continued an old identity", d.DeclarationID)
				}
			}
		}
	}
	if continuedDecls <= 7 {
		t.Fatalf("expected keyless declarations of continued owners to continue, got %d continued declarations", continuedDecls)
	}
	if int(r.Continued) != continuedDecls+continuedSites {
		t.Fatalf("continued %d, want %d declarations plus %d sites", r.Continued, continuedDecls, continuedSites)
	}
}

func TestMatcherContinuesMovedMethodWithUniqueKey(t *testing.T) {
	first := newFixture(t, "run-1")
	a := first.addFile("src/A.java", fixtureA)
	first.match(a)

	second := newFixture(t, "run-2")
	b := second.addFile("src/A.java", fixtureMoved)
	second.ws.previous[b.Lineage] = first.identities(a)
	r := second.match(b)
	if r.Ambiguous != 0 {
		t.Fatalf("move is not ambiguous: %+v", r)
	}
	for _, name := range []string{"helper", "missing"} {
		if second.methodEntity(b, "A", name) != first.methodEntity(a, "A", name) {
			t.Fatalf("moved %s should continue through its unique key", name)
		}
		if second.method(b, "A", name).Span == first.method(a, "A", name).Span {
			t.Fatalf("fixture did not move %s", name)
		}
	}
	// Parameters have no key; they continue through their owner's identity,
	// kind, name and position even though every byte in the file moved.
	handle := second.method(b, "Handler", "handle")
	prevIDs := first.identities(a)
	for _, pid := range handle.Callable.ParameterIDs {
		d, _ := second.identities(b).Declaration(pid)
		found := false
		for _, p := range prevIDs.Declarations {
			if p.EntityID == d.EntityID && p.Kind == d.Kind && p.Name == d.Name {
				found = true
			}
		}
		if !found {
			t.Fatalf("keyless parameter %s of a continued method was not continued", pid)
		}
	}
}

func TestMatcherOccurrenceNeedsSameEnclosingEntity(t *testing.T) {
	first := newFixture(t, "run-1")
	a := first.addFile("src/A.java", fixtureA)
	first.match(a)
	previous := first.identities(a)
	// Pretend run-1 had given A.run another entity: rule (a) continues that
	// entity for the declaration, so its sites no longer match their
	// recorded enclosing entity and must be reallocated.
	runID := first.method(a, "A", "run").ID
	for i := range previous.Declarations {
		if previous.Declarations[i].DeclarationID == runID {
			previous.Declarations[i].EntityID = graph.ID("entity", "elsewhere")
		}
	}

	second := newFixture(t, "run-2")
	b := second.addFile("src/A.java", fixtureA)
	second.ws.previous[b.Lineage] = previous
	second.match(b)
	current := second.identities(b)
	if second.methodEntity(b, "A", "run") != graph.ID("entity", "elsewhere") {
		t.Fatal("unchanged declaration should continue its previous entity")
	}
	before := map[ir.OccurrenceID]semantic.OccurrenceIdentity{}
	for _, o := range previous.Occurrences {
		before[o.OccurrenceID] = o
	}
	inRun, elsewhere := 0, 0
	for _, o := range current.Occurrences {
		p := before[o.OccurrenceID]
		if o.EnclosingDeclarationID == runID {
			inRun++
			if o.PersistentID == p.PersistentID {
				t.Fatalf("occurrence %s continued under a different enclosing entity", o.OccurrenceID)
			}
		} else {
			elsewhere++
			if o.PersistentID != p.PersistentID {
				t.Fatalf("occurrence %s should continue", o.OccurrenceID)
			}
		}
	}
	if inRun == 0 || elsewhere == 0 {
		t.Fatalf("fixture needs sites inside and outside A.run: %d/%d", inRun, elsewhere)
	}
}

func TestMatcherRejectsInvalidRequests(t *testing.T) {
	f := newFixture(t, "run-1")
	a := f.addFile("src/A.java", fixtureA)
	if _, err := (Matcher{}).Match(t.Context(), semantic.MatchRequest{Files: []semantic.SourceInput{a}}, f.ws); err == nil {
		t.Fatal("missing run key accepted")
	}
	a.Lineage = "not an id"
	if _, err := (Matcher{}).Match(t.Context(), semantic.MatchRequest{Run: f.run, Files: []semantic.SourceInput{a}}, f.ws); err == nil {
		t.Fatal("invalid lineage accepted")
	}
}
