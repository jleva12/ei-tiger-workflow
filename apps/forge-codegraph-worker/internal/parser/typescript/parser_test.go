package typescript

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"

	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
)

func inputFor(path, source string) parser.Input {
	content := []byte(source)
	digest := sha256.Sum256(content)
	return parser.Input{Source: ir.Source{FileID: "test-file", RepositoryID: "repo", SnapshotID: "snapshot", Path: path, ContentSHA256: hex.EncodeToString(digest[:]), SizeBytes: uint64(len(content)), Language: Language, LanguageVersion: "5"}, Content: content, Limits: parser.DefaultLimits()}
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

func TestRegistration(t *testing.T) {
	r := Registration()
	if r.Descriptor.Language != Language || r.Descriptor.FeatureSet != FeatureSet || len(r.Descriptor.Extensions) != 8 {
		t.Fatalf("descriptor: %+v", r.Descriptor)
	}
	limits := parser.DefaultLimits()
	if err := r.Validate(parser.Profile{Language: Language, Version: "5"}, limits); err != nil {
		t.Fatal(err)
	}
	// What project discovery writes for the resolver: path mappings,
	// workspace packages, installed packages and the compiler fingerprint.
	discovered := map[string]string{"projects": `[{"dir":"."}]`, "packages": `[]`, "installs": `[{"dir":".","path":"/cache/npm-x","sha256":"` + strings.Repeat("a", 64) + `"}]`, "compiler": strings.Repeat("0f", 32)}
	if err := r.Validate(parser.Profile{Language: Language, Version: "5", Options: parser.Options{Settings: discovered}}, limits); err != nil {
		t.Fatalf("discovered settings: %v", err)
	}
	for _, profile := range []parser.Profile{
		{Language: Language, Version: "3"},
		{Language: "java", Version: "21"},
		{Language: Language, Version: "5", Options: parser.Options{EnablePreview: true}},
		{Language: Language, Version: "5", Options: parser.Options{Settings: map[string]string{"jsx": "react"}}},
		{Language: Language, Version: "5", Options: parser.Options{Settings: map[string]string{"compiler": "not-a-digest"}}},
		{Language: Language, Version: "5", Options: parser.Options{Settings: map[string]string{"installs": "{"}}},
	} {
		if err := r.Validate(profile, limits); !errors.Is(err, parser.ErrUnsupportedConfig) {
			t.Fatalf("%+v accepted: %v", profile, err)
		}
	}
	if mem, err := r.WorkerMemory(limits); err != nil || mem == 0 {
		t.Fatal(mem, err)
	}
	s, err := r.New()
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestTypeScriptDeclarations(t *testing.T) {
	file := parseFixture(t, "service.ts")
	if file.Coverage.Status != ir.ExtractionComplete {
		t.Fatalf("coverage %s: %+v", file.Coverage.Status, file.Coverage.Issues)
	}
	if file.Producer.Name != "ei-typescript-tree-sitter" || file.Coverage.FeatureSet != FeatureSet {
		t.Fatalf("producer %+v coverage %+v", file.Producer, file.Coverage)
	}
	service := one(t, file, ir.DeclarationClass, "AppService")
	if !hasModifier(service, "export") || service.DocComment == "" || service.Type == nil || len(service.Type.Heritage) != 2 || len(service.AnnotationIDs) != 1 {
		t.Fatalf("class: %+v", service)
	}
	extends, implements := 0, 0
	for _, h := range service.Type.Heritage {
		switch h.Kind {
		case ir.HeritageExtends:
			extends++
			if tr := typeByID(file, h.TypeRefID); tr.Named == nil || tr.Named.Segments[0].Name != "ApiClient" {
				t.Fatalf("extends: %+v", tr)
			}
		case ir.HeritageImplements:
			implements++
		}
	}
	if extends != 1 || implements != 1 {
		t.Fatal(extends, implements)
	}
	users := one(t, file, ir.DeclarationField, "users")
	if users.OwnerID != service.ID || !hasModifier(users, "private") || !hasModifier(users, "readonly") || users.Variable == nil || users.Variable.DeclaredTypeID == "" {
		t.Fatalf("field: %+v", users)
	}
	if tr := typeByID(file, users.Variable.DeclaredTypeID); tr.Kind != ir.TypeArray {
		t.Fatalf("users type: %+v", tr)
	}
	if count := one(t, file, ir.DeclarationField, "count"); !hasModifier(count, "static") {
		t.Fatalf("static field: %+v", count)
	}
	ctor := one(t, file, ir.DeclarationConstructor, "constructor")
	if ctor.OwnerID != service.ID || ctor.Callable == nil || len(ctor.Callable.ParameterIDs) != 1 {
		t.Fatalf("constructor: %+v", ctor)
	}
	load := one(t, file, ir.DeclarationMethod, "load")
	if load.OwnerID != service.ID || load.Callable == nil || load.Callable.ReturnTypeID == "" || len(load.Callable.ParameterIDs) != 1 || !hasModifier(load, "async") {
		t.Fatalf("method: %+v", load)
	}
	if tr := typeByID(file, load.Callable.ReturnTypeID); tr.Named == nil || tr.Named.Segments[0].Name != "Promise" || len(tr.Named.Segments[0].TypeArguments) != 1 {
		t.Fatalf("return type: %+v", tr)
	}
	if runs := declIndex(file)[declKey{ir.DeclarationMethod, "run"}]; len(runs) != 2 {
		t.Fatalf("class and interface run methods: %d", len(runs))
	}
	if iface := one(t, file, ir.DeclarationInterface, "Runnable"); iface.Type == nil || iface.Type.Form != ir.TypeTopLevel {
		t.Fatalf("interface: %+v", iface)
	}
	mode := one(t, file, ir.DeclarationEnum, "Mode")
	for _, name := range []string{"Fast", "Slow"} {
		if c := one(t, file, ir.DeclarationEnumConstant, name); c.OwnerID != mode.ID {
			t.Fatalf("enum constant %s: %+v", name, c)
		}
	}
	one(t, file, ir.DeclarationTypeAlias, "Handler")
	config := one(t, file, ir.DeclarationNamespace, "Config")
	if timeout := one(t, file, ir.DeclarationVariable, "timeout"); timeout.OwnerID != config.ID {
		t.Fatalf("namespace member: %+v", timeout)
	}
	create := one(t, file, ir.DeclarationFunction, "create")
	if create.Callable == nil || len(create.Callable.TypeParameterIDs) != 1 || len(create.Callable.ParameterIDs) != 1 || create.Callable.ReturnTypeID == "" {
		t.Fatalf("function: %+v", create)
	}
	one(t, file, ir.DeclarationTypeParameter, "T")
	one(t, file, ir.DeclarationParameter, "seed")
	if handler := one(t, file, ir.DeclarationFunction, "handler"); handler.OwnerID != "" || handler.Callable == nil || len(handler.Callable.ParameterIDs) != 1 {
		t.Fatalf("function-valued const: %+v", handler)
	}
	if user := one(t, file, ir.DeclarationLocal, "user"); user.Variable == nil || user.Variable.InitializerID == "" {
		t.Fatalf("local: %+v", user)
	}
	run := declIndex(file)[declKey{ir.DeclarationMethod, "run"}]
	if u := one(t, file, ir.DeclarationParameter, "u"); u.OwnerID != run[0].ID && u.OwnerID != run[1].ID {
		t.Fatalf("lambda parameter is owned by the enclosing method: %+v", u)
	}
	kinds := map[ir.ExpressionKind]int{}
	for _, x := range file.Expressions {
		kinds[x.Kind]++
	}
	if kinds[ir.ExpressionObjectLiteral] != 1 || kinds[ir.ExpressionUnknown] != 0 {
		t.Fatalf("expression kinds: %v", kinds)
	}
	typeKinds := map[ir.TypeKind]int{}
	for _, tr := range file.Types {
		typeKinds[tr.Kind]++
	}
	if typeKinds[ir.TypeFunction] != 1 || typeKinds[ir.TypeUnknown] != 0 {
		t.Fatalf("type kinds: %v", typeKinds)
	}
}

func TestTypeScriptImportsAndExports(t *testing.T) {
	file := parseFixture(t, "service.ts")
	type imp struct{ module, name, alias string }
	var got []imp
	for _, i := range file.Imports {
		got = append(got, imp{i.Module, i.Name.Segments[0].Text, i.Alias})
	}
	want := []imp{{"./api", "ApiClient", ""}, {"./api", "User", ""}, {"./util", "*", "util"}, {"react", "default", "React"}}
	if len(got) != len(want) {
		t.Fatalf("imports: %+v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("import %d: got %+v want %+v", i, got[i], want[i])
		}
	}
	defaultExport := false
	for _, r := range file.References {
		if r.Kind == ir.ReferenceName && r.Name.Segments[0].Text == "AppService" {
			defaultExport = true
		}
	}
	if !defaultExport {
		t.Fatal("export default AppService is not a reference")
	}
	if service := one(t, file, ir.DeclarationClass, "AppService"); !hasModifier(service, "default") {
		t.Fatalf("export default AppService does not mark the class: %+v", service.Modifiers)
	}
}

func TestTypeScriptCallsReferencesAndTypes(t *testing.T) {
	file := parseFixture(t, "service.ts")
	calls := callNames(file)
	for _, want := range []string{"method:constructor", "method:get", "method:push", "method:log", "method:forEach", "object_creation:", "method:log"} {
		if calls[want] == 0 {
			t.Fatalf("missing call %s in %v", want, calls)
		}
	}
	if calls["object_creation:"] != 2 {
		t.Fatalf("constructions: %v", calls)
	}
	var get ir.Call
	for _, c := range file.Calls {
		if c.Name == "get" {
			get = c
		}
	}
	if get.ReceiverID == "" || len(get.TypeArgumentIDs) != 1 || len(get.Arguments) != 1 {
		t.Fatalf("this.client.get<User>(...): %+v", get)
	}
	for _, c := range file.Calls {
		if c.Kind == ir.CallObjectCreation {
			if tr := typeByID(file, c.ConstructedTypeID); tr.Named == nil {
				t.Fatalf("constructed type: %+v", c)
			}
		}
	}
	members, names := 0, 0
	for _, r := range file.References {
		switch r.Kind {
		case ir.ReferenceMember:
			members++
			if r.ReceiverID == "" {
				t.Fatalf("member reference without receiver: %+v", r)
			}
		case ir.ReferenceName:
			names++
		}
	}
	if members == 0 || names == 0 {
		t.Fatal(members, names)
	}
	if len(file.Lambdas) != 1 || len(file.Lambdas[0].ParameterIDs) != 1 {
		t.Fatalf("lambdas: %+v", file.Lambdas)
	}
	if handler := one(t, file, ir.DeclarationFunction, "handler"); handler.BodyScopeID == "" {
		t.Fatalf("function literal body scope: %+v", handler)
	}
	if len(file.Annotations) != 1 {
		t.Fatalf("annotations: %+v", file.Annotations)
	}
	if tr := typeByID(file, file.Annotations[0].TypeRefID); tr.Named == nil || tr.Named.Segments[0].Name != "Component" {
		t.Fatalf("decorator type: %+v", tr)
	}
	roles := map[ir.TypeUseRole]int{}
	for _, u := range file.TypeUses {
		roles[u.Role]++
	}
	for _, role := range []ir.TypeUseRole{ir.TypeUseHeritage, ir.TypeUseField, ir.TypeUseParameter, ir.TypeUseReturn, ir.TypeUseGenericArgument, ir.TypeUseConstruction, ir.TypeUseAnnotation, ir.TypeUseAlias, ir.TypeUseBound} {
		if roles[role] == 0 {
			t.Fatalf("missing type use role %s: %v", role, roles)
		}
	}
	if roles[ir.TypeUseHeritage] != 2 {
		t.Fatalf("heritage uses: %v", roles)
	}
	primitives := 0
	for _, tr := range file.Types {
		if tr.Kind == ir.TypePrimitive {
			primitives++
		}
	}
	if primitives == 0 {
		t.Fatal("no predefined types")
	}
}

func TestTSX(t *testing.T) {
	file := parseFixture(t, "App.tsx")
	if file.Coverage.Status != ir.ExtractionComplete {
		t.Fatalf("coverage %s: %+v", file.Coverage.Status, file.Coverage.Issues)
	}
	app := one(t, file, ir.DeclarationFunction, "App")
	if !hasModifier(app, "export") || app.Callable == nil || len(app.Callable.ParameterIDs) != 1 {
		t.Fatalf("component: %+v", app)
	}
	names := map[string]bool{}
	memberNames := map[string]bool{}
	for _, r := range file.References {
		if r.Kind == ir.ReferenceName {
			names[r.Name.Segments[0].Text] = true
		} else {
			memberNames[r.Name.Segments[0].Text] = true
		}
	}
	if !names["Button"] || names["div"] || !memberNames["Header"] || !memberNames["title"] {
		t.Fatalf("jsx references: names=%v members=%v", names, memberNames)
	}
	calls := callNames(file)
	if calls["method:useState"] != 1 || calls["method:setCount"] != 1 {
		t.Fatalf("calls: %v", calls)
	}
	if len(file.Lambdas) != 1 {
		t.Fatalf("lambdas: %+v", file.Lambdas)
	}
	for _, name := range []string{"count", "setCount"} {
		one(t, file, ir.DeclarationLocal, name)
	}
	markup := 0
	for _, x := range file.Expressions {
		if x.Kind == ir.ExpressionMarkup {
			markup++
		}
		if x.Kind == ir.ExpressionUnknown {
			t.Fatalf("unknown expression: %+v", x)
		}
	}
	if markup != 3 {
		t.Fatalf("markup expressions: %d", markup)
	}
	structural, components := 0, 0
	for _, tr := range file.Types {
		if tr.Kind == ir.TypeStructural {
			structural++
		}
	}
	for _, u := range file.TypeUses {
		if u.Role == ir.TypeUseComponent && u.ParentTypeID != "" {
			components++
		}
	}
	if structural != 1 || components != 1 {
		t.Fatalf("structural types %d, components %d", structural, components)
	}
}

func TestJavaScript(t *testing.T) {
	file := parseFixture(t, "store.js")
	if file.Coverage.Status != ir.ExtractionComplete {
		t.Fatalf("coverage %s: %+v", file.Coverage.Status, file.Coverage.Issues)
	}
	modules := map[string]string{}
	for _, i := range file.Imports {
		modules[i.Module] = i.Name.Segments[0].Text + "/" + i.Alias
	}
	if modules["path"] != "*/path" || modules["./helper.js"] != "helper/" {
		t.Fatalf("imports: %v", modules)
	}
	store := one(t, file, ir.DeclarationClass, "Store")
	if !hasModifier(store, "export") {
		t.Fatalf("class: %+v", store)
	}
	if ctor := one(t, file, ir.DeclarationConstructor, "constructor"); ctor.OwnerID != store.ID {
		t.Fatalf("constructor: %+v", ctor)
	}
	if save := one(t, file, ir.DeclarationMethod, "save"); save.OwnerID != store.ID || save.Callable == nil || save.Callable.ReturnTypeID != "" {
		t.Fatalf("method: %+v", save)
	}
	main := one(t, file, ir.DeclarationFunction, "main")
	if !hasModifier(main, "export") || !hasModifier(main, "default") {
		t.Fatalf("default export function: %+v", main)
	}
	calls := callNames(file)
	if calls["method:helper"] != 1 || calls["method:basename"] != 1 || calls["object_creation:"] != 1 || calls["method:require"] != 1 {
		t.Fatalf("calls: %v", calls)
	}
	for _, tr := range file.Types {
		if tr.Kind == ir.TypeNamed && tr.Named.Segments[0].Name == "Store" {
			return
		}
	}
	t.Fatal("constructed type Store is not recorded")
}

func TestMalformedInputIsPartial(t *testing.T) {
	p := newTestParser(t)
	file := parseTest(t, p, inputFor("src/broken.ts", "export class Broken {\n  run() { return 1 +; }\n}\n"))
	if file.Coverage.Status != ir.ExtractionPartial {
		t.Fatalf("coverage %s", file.Coverage.Status)
	}
	found := false
	for _, issue := range file.Coverage.Issues {
		if issue.Reason == ir.CoverageParseError {
			found = true
		}
	}
	if !found {
		t.Fatalf("issues: %+v", file.Coverage.Issues)
	}
	one(t, file, ir.DeclarationClass, "Broken")
}

func TestLimitsAndInputs(t *testing.T) {
	p := newTestParser(t)
	in := inputFor("src/big.ts", strings.Repeat("const x = 1;\n", 200))
	in.Limits.MaxSourceBytes = 16
	if _, err := p.Parse(context.Background(), in); !errors.Is(err, parser.ErrLimitExceeded) {
		t.Fatalf("source budget: %v", err)
	}
	in = inputFor("src/big.ts", strings.Repeat("const x = 1;\n", 200))
	in.Limits.MaxIRRecords = 5
	if _, err := p.Parse(context.Background(), in); !errors.Is(err, parser.ErrLimitExceeded) {
		t.Fatalf("record budget: %v", err)
	}
	in = inputFor("src/a.ts", "const a = 1;")
	in.Source.LanguageVersion = "3"
	if _, err := p.Parse(context.Background(), in); !errors.Is(err, parser.ErrUnsupportedConfig) {
		t.Fatalf("version: %v", err)
	}
	in = inputFor("src/a.ts", "const a = 1;")
	in.Source.Language = "java"
	if _, err := p.Parse(context.Background(), in); err == nil {
		t.Fatal("foreign language accepted")
	}
	in = inputFor("src/a.ts", "const a = 1;")
	in.Source.ContentSHA256 = strings.Repeat("0", 64)
	if _, err := p.Parse(context.Background(), in); err == nil {
		t.Fatal("digest mismatch accepted")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := p.Parse(ctx, inputFor("src/a.ts", "const a = 1;")); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancellation: %v", err)
	}
	if err := p.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := p.Parse(context.Background(), inputFor("src/a.ts", "const a = 1;")); err == nil {
		t.Fatal("closed parser accepted input")
	}
	if err := p.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestDeterminismAndUniqueIDs(t *testing.T) {
	p := newTestParser(t)
	for _, name := range []string{"service.ts", "App.tsx", "store.js"} {
		in := inputFor("src/"+name, fixture(t, name))
		first := parseTest(t, p, in)
		second := parseTest(t, p, in)
		a, _ := json.Marshal(first)
		b, _ := json.Marshal(second)
		if !bytes.Equal(a, b) {
			t.Fatalf("%s: non-deterministic output", name)
		}
		seen := map[string]bool{}
		note := func(kind, id string) {
			if seen[kind+id] {
				t.Fatalf("%s: duplicate %s %s", name, kind, id)
			}
			seen[kind+id] = true
		}
		for _, d := range first.Declarations {
			note("decl", string(d.ID))
		}
		for _, x := range first.Expressions {
			note("expr", string(x.ID))
		}
		for _, c := range first.Calls {
			note("occ", string(c.Occurrence.ID))
		}
		for _, r := range first.References {
			note("occ", string(r.Occurrence.ID))
		}
		for _, u := range first.TypeUses {
			note("occ", string(u.Occurrence.ID))
		}
		for _, i := range first.Imports {
			note("occ", string(i.Occurrence.ID))
		}
		for _, l := range first.Lambdas {
			note("occ", string(l.Occurrence.ID))
		}
		for _, a := range first.Annotations {
			note("occ", string(a.Occurrence.ID))
		}
	}
}

const reexportsSource = `import { ApiClient } from "./api";
import Default from "./default";
export { ApiClient };
export { Default as default };
export { Helper, Other as Renamed } from "./helper";
export * from "./util";
export * as models from "./models";
const { readFile, writeFile: write } = require("./fs");
const path = require("path");
export function local() {
  return readFile(path.join("a"), write);
}
export { local as renamed };
`

func TestReExportsAndRequireBindings(t *testing.T) {
	file := parseTest(t, newTestParser(t), inputFor("src/index.ts", reexportsSource))
	if file.Coverage.Status != ir.ExtractionComplete {
		t.Fatalf("coverage %s: %+v", file.Coverage.Status, file.Coverage.Issues)
	}
	type imp struct {
		kind                ir.ImportKind
		module, name, alias string
	}
	var got []imp
	for _, i := range file.Imports {
		got = append(got, imp{i.Kind, i.Module, i.Name.Segments[0].Text, i.Alias})
	}
	want := []imp{
		{ir.ImportSingleType, "./api", "ApiClient", ""},
		{ir.ImportSingleType, "./default", "default", "Default"},
		{ir.ImportReExport, "./helper", "Helper", ""},
		{ir.ImportReExport, "./helper", "Other", "Renamed"},
		{ir.ImportReExport, "./util", "*", ""},
		{ir.ImportReExport, "./models", "*", "models"},
		{ir.ImportSingleType, "./fs", "readFile", ""},
		{ir.ImportSingleType, "./fs", "writeFile", "write"},
		{ir.ImportTypeOnDemand, "path", "*", "path"},
		// export { ApiClient } and export { Default as default } forward
		// imports; export { local as renamed } re-exports a local binding.
		{ir.ImportReExport, "./api", "ApiClient", "ApiClient"},
		{ir.ImportReExport, "./default", "default", "default"},
		{ir.ImportReExport, "", "local", "renamed"},
	}
	if len(got) != len(want) {
		t.Fatalf("imports: %+v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("import %d: got %+v want %+v", i, got[i], want[i])
		}
	}
	for _, name := range []string{"readFile", "write", "writeFile", "path"} {
		if ds := declIndex(file)[declKey{ir.DeclarationVariable, name}]; len(ds) != 0 {
			t.Fatalf("require binding %s declared as a variable: %+v", name, ds)
		}
	}
	one(t, file, ir.DeclarationFunction, "local")
	calls := callNames(file)
	if calls["method:require"] != 2 || calls["method:readFile"] != 1 || calls["method:join"] != 1 {
		t.Fatalf("calls: %v", calls)
	}
}

func TestRealWorldSyntaxShapes(t *testing.T) {
	source := `const re = /ab+c/gi;
let a = 1, b = 2;
[a, b] = [b, a];
({ a, b = 3 } = { a: 4 });
type Shape =
  | { kind: "circle"; radius: number }
  | { kind: "square"; side: number };
type One = | Circle;
export class Service {
  constructor(private readonly client: Client, public name: string, plain: number) {}
}
`
	file := parseTest(t, newTestParser(t), inputFor("src/shapes.ts", source))
	if file.Coverage.Status != ir.ExtractionComplete {
		t.Fatalf("coverage %s: %+v", file.Coverage.Status, file.Coverage.Issues)
	}
	for _, x := range file.Expressions {
		if x.Kind == ir.ExpressionUnknown {
			t.Fatalf("unknown expression: %+v", x)
		}
	}
	for _, tr := range file.Types {
		if tr.Kind == ir.TypeUnknown {
			t.Fatalf("unknown type: %+v", tr)
		}
	}
	writes := 0
	for _, r := range file.References {
		if r.Access == ir.AccessWrite {
			writes++
		}
	}
	if writes < 4 {
		t.Fatalf("destructuring assignment writes: %d", writes)
	}
	one := one(t, file, ir.DeclarationTypeAlias, "One")
	if len(one.Type.Heritage) != 1 {
		t.Fatalf("alias of a named type has that type as its base: %+v", one)
	}
	service := one2(t, file, ir.DeclarationClass, "Service")
	fields := declIndex(file)
	for _, name := range []string{"client", "name"} {
		f := fields[declKey{ir.DeclarationField, name}]
		if len(f) != 1 || f[0].OwnerID != service.ID || f[0].Variable == nil || f[0].Variable.DeclaredTypeID == "" {
			t.Fatalf("parameter property %s: %+v", name, f)
		}
	}
	if len(fields[declKey{ir.DeclarationField, "plain"}]) != 0 {
		t.Fatal("plain parameter declared as a field")
	}
	if !hasModifier(fields[declKey{ir.DeclarationField, "client"}][0], "private") || !hasModifier(fields[declKey{ir.DeclarationField, "client"}][0], "readonly") {
		t.Fatalf("parameter property modifiers: %+v", fields[declKey{ir.DeclarationField, "client"}][0].Modifiers)
	}
}

func one2(t testing.TB, file ir.SourceFile, kind ir.DeclarationKind, name string) ir.Declaration {
	t.Helper()
	return one(t, file, kind, name)
}

func TestTypeAliasMembers(t *testing.T) {
	source := `type Base = { id: string };
type Props = Base & Readonly<{ user: User; count?: number }> & { render(): void };
type Plain = { name: string };
type Union = { a: 1 } | { b: 2 };
`
	file := parseTest(t, newTestParser(t), inputFor("src/alias.ts", source))
	props := one(t, file, ir.DeclarationTypeAlias, "Props")
	members := map[string]ir.DeclarationKind{}
	for _, d := range file.Declarations {
		if d.OwnerID == props.ID {
			members[d.Name] = d.Kind
		}
	}
	if members["user"] != ir.DeclarationField || members["count"] != ir.DeclarationField || members["render"] != ir.DeclarationMethod {
		t.Fatalf("alias members: %v", members)
	}
	if len(props.Type.Heritage) != 1 || props.Type.Heritage[0].Kind != ir.HeritageExtends {
		t.Fatalf("alias heritage: %+v", props.Type.Heritage)
	}
	if tr := typeByID(file, props.Type.Heritage[0].TypeRefID); tr.Named == nil || tr.Named.Segments[0].Name != "Base" {
		t.Fatalf("alias base: %+v", tr)
	}
	plain := one(t, file, ir.DeclarationTypeAlias, "Plain")
	if f := one(t, file, ir.DeclarationField, "name"); f.OwnerID != plain.ID {
		t.Fatalf("plain alias member: %+v", f)
	}
	// A union's object constituents contribute members too: the syntax
	// tier binds a member to the first constituent declaring it.
	union := one(t, file, ir.DeclarationTypeAlias, "Union")
	unionMembers := 0
	for _, d := range file.Declarations {
		if d.OwnerID == union.ID {
			unionMembers++
		}
	}
	if unionMembers != 2 {
		t.Fatalf("union members: %d", unionMembers)
	}
}

func TestGeneratedSourcesAreDeclined(t *testing.T) {
	long := strings.Repeat("var a=1;", 10000) // 80,000 bytes on one line
	cases := map[string]string{
		"/* @generated by tool */\nexport const x = 1;\n":                         "marker",
		"// Code generated by protoc-gen-ts. DO NOT EDIT.\nexport const y = 2;\n": "marker",
		"export const z = 3;\n//# sourceMappingURL=z.js.map\n":                    "source map",
		long: "minified",
		strings.Repeat("const line = 1;\n", 6000) + strings.Repeat("x", 60000) + "\n": "minified",
		"(function(y,v){typeof exports==\"object\"&&typeof module!=\"undefined\"?v(exports):typeof define==\"function\"&&define.amd?define([\"exports\"],v):v(y.lib={})})(this,function(y){\n\"use strict\";y.x=1});\n": "UMD",
		strings.Repeat(strings.Repeat("var a=1;", 250)+"\n", 14): "minified in 14 lines",
	}
	p := newTestParser(t)
	for source, why := range cases {
		if reason := generated([]byte(source)); reason == "" {
			t.Fatalf("%s: not detected", why)
		}
		if _, err := p.Parse(context.Background(), inputFor("src/gen.ts", source)); !errors.Is(err, parser.ErrGeneratedSource) {
			t.Fatalf("%s: %v", why, err)
		}
	}
	for _, source := range []string{
		fixture(t, "service.ts"),
		strings.Repeat("export function f() {\n  return 1;\n}\n", 4000), // large but ordinary
		"// eslint-disable-next-line\nexport const ok = 1;\n",
		"const header = \"// DO NOT EDIT: generated by buildWasm\";\nexport function build() { return header; }\n",
	} {
		if reason := generated([]byte(source)); reason != "" {
			t.Fatalf("ordinary source declined: %s", reason)
		}
	}
	// A minifier's file name, or a small file nearly all on one line.
	small := strings.Repeat("var a=1;", 1500) // 12,000 bytes on one line
	for path, source := range map[string]string{"static/js/embed.min.js": "export const x = 1;\n", "src/widget.js": small, "vendor/lib.umd.js": "export const x = 1;\n"} {
		if _, err := p.Parse(context.Background(), inputFor(path, source)); !errors.Is(err, parser.ErrGeneratedSource) {
			t.Fatalf("%s: %v", path, err)
		}
	}
	if reason := generatedFile("src/ok.js", []byte(strings.Repeat("const a = 1;\n", 1000))); reason != "" {
		t.Fatalf("ordinary source declined: %s", reason)
	}
}
