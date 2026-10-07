package ingestion

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/parser"
	"ei-aitiger-codegraph/pkg/semantic"
	"ei-aitiger-codegraph/worker/internal/discovery"
)

func defaultDiscoveryLimits() discovery.Limits { return discovery.DefaultLimits() }

type fakeResolver struct {
	language string
	requests []semantic.ResolveRequest
	result   semantic.ResolutionResult
	err      error
}

func (r *fakeResolver) Resolve(_ context.Context, req semantic.ResolveRequest, _ semantic.Workspace) (semantic.ResolutionResult, error) {
	r.requests = append(r.requests, req)
	return r.result, r.err
}
func (r *fakeResolver) Version() string      { return r.language + "-1" }
func (r *fakeResolver) PolicyDigest() string { return "sha256:" + strings.Repeat("a", 64) }

func inventory() map[string]bc.SourceSet {
	return map[string]bc.SourceSet{
		"api-main": {ID: "api-main", Language: "java", LanguageVersion: "21"},
		"api-test": {ID: "api-test", TargetRelease: 21}, // legacy empty language is Java
		"web":      {ID: "web", Language: "typescript", LanguageVersion: "5.4"},
	}
}

func TestRouteContextsGroupsByLanguage(t *testing.T) {
	resolvers := map[string]semantic.Resolver{"java": &fakeResolver{language: "java"}, "typescript": &fakeResolver{language: "typescript"}}
	routed, err := routeContexts([]bc.SourceSetID{"web", "api-test", "api-main"}, inventory(), resolvers)
	if err != nil {
		t.Fatal(err)
	}
	if len(routed) != 2 || len(routed["java"]) != 2 || routed["java"][0] != "api-main" || routed["java"][1] != "api-test" || len(routed["typescript"]) != 1 || routed["typescript"][0] != "web" {
		t.Fatalf("routing: %v", routed)
	}
	if _, err = routeContexts([]bc.SourceSetID{"missing"}, inventory(), resolvers); err == nil || !strings.Contains(err.Error(), "not in the build inventory") {
		t.Fatalf("unknown context: %v", err)
	}
	if _, err = routeContexts([]bc.SourceSetID{"web"}, inventory(), map[string]semantic.Resolver{"java": resolvers["java"]}); err == nil || !strings.Contains(err.Error(), "no resolver for language typescript") {
		t.Fatalf("missing resolver: %v", err)
	}
}

func TestResolveAttributesEachLanguageWithItsOwnResolver(t *testing.T) {
	java := &fakeResolver{language: "java", result: semantic.ResolutionResult{Symbols: 10, Resolved: 8, Unresolved: 2, Warnings: []string{"Lombok could not run."}}}
	ts := &fakeResolver{language: "typescript", result: semantic.ResolutionResult{Symbols: 3, Resolved: 1, Ambiguous: 1, Unsupported: 1}}
	p := &Pipeline{config: Config{Resolvers: map[string]semantic.Resolver{"java": java, "typescript": ts}, ParserLimits: parser.DefaultLimits(), BuildLimits: bc.DefaultLimits()}}
	key := deployment.RunKey{RepositoryID: "repo", RunID: "run"}
	total, err := p.resolve(context.Background(), nil, key, strings.Repeat("c", 40), "/checkout", []bc.SourceSetID{"web", "api-main", "api-test"}, inventory(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(total, semantic.ResolutionResult{Symbols: 13, Resolved: 9, Unresolved: 2, Ambiguous: 1, Unsupported: 1, Warnings: []string{"Lombok could not run."}}) {
		t.Fatalf("totals: %+v", total)
	}
	if len(java.requests) != 1 || len(java.requests[0].Contexts) != 2 || len(ts.requests) != 1 || ts.requests[0].Contexts[0] != "web" {
		t.Fatalf("each resolver sees only its contexts: java=%+v ts=%+v", java.requests, ts.requests)
	}
	if java.requests[0].Run != key || java.requests[0].CheckoutPath != "/checkout" || java.requests[0].SyntaxLimits != parser.DefaultLimits() {
		t.Fatalf("request identity: %+v", java.requests[0])
	}
	// A resolver failure leaves that language unresolved, says so, and
	// marks its contexts to resolve again; the other languages still run.
	ts.err = errors.New("tsc missing")
	total, err = p.resolve(context.Background(), nil, key, strings.Repeat("c", 40), "/checkout", []bc.SourceSetID{"web", "api-main"}, inventory(), nil)
	if err != nil || total.Resolved != 8 || !reflect.DeepEqual(total.Degraded, []string{"web"}) || len(total.Warnings) != 2 || !strings.Contains(total.Warnings[1], "The typescript resolver failed (tsc missing)") {
		t.Fatalf("degraded language: %+v %v", total, err)
	}
	// An integrity failure is a defect, not a degraded language: it stops
	// the run and names the language.
	ts.err = fmt.Errorf("%w: symbol table", semantic.ErrIntegrity)
	if _, err = p.resolve(context.Background(), nil, key, strings.Repeat("c", 40), "/checkout", []bc.SourceSetID{"web"}, inventory(), nil); err == nil || !strings.HasPrefix(err.Error(), "typescript: ") || !errors.Is(err, semantic.ErrIntegrity) {
		t.Fatalf("integrity failure: %v", err)
	}
}

func TestResolverVersionsAndDigestCoverEveryLanguage(t *testing.T) {
	resolvers := map[string]semantic.Resolver{"typescript": &fakeResolver{language: "typescript"}, "java": &fakeResolver{language: "java"}}
	versions := ResolverVersions(resolvers)
	if len(versions) != 2 || versions[0].Language != "java" || versions[0].Version != "java-1" || versions[1].Language != "typescript" {
		t.Fatalf("versions: %+v", versions)
	}
	one := Digest("registry", ResolverVersions(map[string]semantic.Resolver{"java": resolvers["java"]}), bc.DefaultLimits(), defaultDiscoveryLimits(), parser.DefaultLimits())
	two := Digest("registry", versions, bc.DefaultLimits(), defaultDiscoveryLimits(), parser.DefaultLimits())
	if !strings.HasPrefix(one, "sha256:") || one == two {
		t.Fatalf("adding a language must change the analysis digest: %s %s", one, two)
	}
	if again := Digest("registry", versions, bc.DefaultLimits(), defaultDiscoveryLimits(), parser.DefaultLimits()); again != two {
		t.Fatal("digest must be deterministic")
	}
}
