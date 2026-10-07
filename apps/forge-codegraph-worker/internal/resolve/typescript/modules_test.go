package typescript

import (
	"testing"

	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

const userModelSource = `export interface User {
  name: string;
}
export class UserModel {
  save(): void {}
}
`
const utilSource = `export function format(u: string): string {
  return u;
}
`
const barrelSource = `export { ApiClient as Client } from "./api";
export { default } from "./api";
export * from "./util";
export * as models from "./models/user";
import { format } from "./util";
export { format as fmt };
export function version(): string {
  return "1";
}
export { version as ver };
`
const consumerSource = `import { Client, format, fmt, models, ver } from "@/index";
import Api from "@/index";
import { helper } from "@acme/core";
import { deep } from "@acme/core/src/deep.js";
import { missing } from "@/nowhere";
import { gone } from "./util";

export class App {
  run(): void {
    new Client().get("/x");
    format("a");
    fmt("b");
    ver();
    new models.UserModel().save();
    new Api().get("/y");
    helper();
    deep();
    missing();
    gone();
  }
}
`
const coreIndexSource = `export function helper(): void {}
`
const coreDeepSource = `export function deep(): void {}
`

func projectSettings() Settings {
	return Settings{
		Projects: []Project{{Dir: "web", Paths: map[string][]string{"@/*": {"web/src/*"}}}},
		Packages: []Package{{Name: "@acme/core", Dir: "packages/core", Entries: []string{"packages/core/src/index.ts"}}},
	}
}

func TestReExportsPathsAndPackages(t *testing.T) {
	f := newFixture(t)
	f.configure(projectSettings())
	f.add("api", "web/src/api.ts", apiSource, true, true)
	f.add("user", "web/src/models/user.ts", userModelSource, true, true)
	f.add("util", "web/src/util.ts", utilSource, true, true)
	f.add("index", "web/src/index.ts", barrelSource, true, true)
	f.add("app", "web/src/app.ts", consumerSource, true, true)
	f.add("core", "packages/core/src/index.ts", coreIndexSource, true, true)
	f.add("deep", "packages/core/src/deep.ts", coreDeepSource, true, true)
	f.resolve()

	apiClient := f.symbol("api", ir.DeclarationClass, "ApiClient")
	get := f.symbol("api", ir.DeclarationMethod, "get")
	format := f.symbol("util", ir.DeclarationFunction, "format")
	expectResolved(t, f.lookup("app", "new Client()", 0), semantic.LookupCall, apiClient)
	expectResolved(t, f.lookup("app", `new Client().get("/x")`, 0), semantic.LookupCall, get)
	expectResolved(t, f.lookup("app", `format("a")`, 0), semantic.LookupCall, format)
	expectResolved(t, f.lookup("app", `fmt("b")`, 0), semantic.LookupCall, format)
	expectResolved(t, f.lookup("app", "ver()", 0), semantic.LookupCall, f.symbol("index", ir.DeclarationFunction, "version"))
	expectResolved(t, f.lookup("app", "new models.UserModel()", 0), semantic.LookupCall, f.symbol("user", ir.DeclarationClass, "UserModel"))
	expectResolved(t, f.lookup("app", "new models.UserModel().save()", 0), semantic.LookupCall, f.symbol("user", ir.DeclarationMethod, "save"))
	expectResolved(t, f.lookup("app", "new Api()", 0), semantic.LookupCall, apiClient)
	expectResolved(t, f.lookup("app", "helper()", 0), semantic.LookupCall, f.symbol("core", ir.DeclarationFunction, "helper"))
	expectResolved(t, f.lookup("app", "deep()", 0), semantic.LookupCall, f.symbol("deep", ir.DeclarationFunction, "deep"))
	expectUnresolved(t, f.lookup("app", "missing()", 0), semantic.CauseAnalysisLimitation, "module_not_found")
	expectUnresolved(t, f.lookup("app", "gone()", 0), semantic.CauseAnalysisLimitation, "export_not_found")
	// The barrel itself binds nothing but its own import.
	for _, l := range f.w.lookups["index"] {
		if l.Status != semantic.LookupResolved && l.Kind != semantic.LookupMember {
			t.Fatalf("barrel lookup: %+v", l)
		}
	}
}

func TestReExportsThroughUnchangedBarrel(t *testing.T) {
	f := newFixture(t)
	f.configure(projectSettings())
	f.add("api", "web/src/api.ts", apiSource, false, false)
	f.add("user", "web/src/models/user.ts", userModelSource, false, false)
	f.add("util", "web/src/util.ts", utilSource, false, false)
	f.add("index", "web/src/index.ts", barrelSource, false, false)
	f.add("app", "web/src/app.ts", consumerSource, true, true)
	f.add("core", "packages/core/src/index.ts", coreIndexSource, false, false)
	f.add("deep", "packages/core/src/deep.ts", coreDeepSource, false, false)
	f.resolve()
	expectResolved(t, f.lookup("app", "new Client()", 0), semantic.LookupCall, f.symbol("api", ir.DeclarationClass, "ApiClient"))
	expectResolved(t, f.lookup("app", `fmt("b")`, 0), semantic.LookupCall, f.symbol("util", ir.DeclarationFunction, "format"))
	expectResolved(t, f.lookup("app", "new Api()", 0), semantic.LookupCall, f.symbol("api", ir.DeclarationClass, "ApiClient"))
	expectResolved(t, f.lookup("app", "deep()", 0), semantic.LookupCall, f.symbol("deep", ir.DeclarationFunction, "deep"))
}

func TestReExportCycleTerminates(t *testing.T) {
	f := newFixture(t)
	f.add("a", "web/src/a.ts", "export * from \"./b\";\nexport const shared = 1;\n", true, true)
	f.add("b", "web/src/b.ts", "export * from \"./a\";\n", true, true)
	f.add("c", "web/src/c.ts", "import { nothing, shared } from \"./b\";\nexport function use(): number { return nothing() + shared; }\n", true, true)
	f.resolve()
	expectUnresolved(t, f.lookup("c", "nothing()", 0), semantic.CauseAnalysisLimitation, "export_not_found")
	expectResolved(t, f.lookup("c", "shared", 0), semantic.LookupMember, f.symbol("a", ir.DeclarationVariable, "shared"))
}

func TestSettingsRoundTrip(t *testing.T) {
	options, err := EncodeSettings(projectSettings())
	must(t, err)
	decoded, err := DecodeSettings(options)
	must(t, err)
	if len(decoded.Projects) != 1 || decoded.Projects[0].Paths["@/*"][0] != "web/src/*" || len(decoded.Packages) != 1 || decoded.Packages[0].Name != "@acme/core" {
		t.Fatalf("round trip: %+v", decoded)
	}
	if empty, err := EncodeSettings(Settings{}); err != nil || len(empty) != 0 {
		t.Fatalf("empty settings: %v %v", empty, err)
	}
	if _, err := DecodeSettings(map[string]string{SettingProjects: "nope"}); err == nil {
		t.Fatal("invalid projects accepted")
	}
	if err := ValidateSettings(map[string]string{"other": "{}"}); err == nil {
		t.Fatal("unknown setting accepted")
	}
	p := &Project{Dir: ".", Paths: map[string][]string{"@/*": {"src/*"}, "@lib/*": {"lib/*", "vendor/*"}, "config": {"src/config.ts"}}}
	if targets, ok := p.mapped("@lib/x/y"); !ok || len(targets) != 2 || targets[0] != "lib/x/y" || targets[1] != "vendor/x/y" {
		t.Fatalf("mapped: %v %v", targets, ok)
	}
	if targets, ok := p.mapped("config"); !ok || targets[0] != "src/config.ts" {
		t.Fatalf("exact mapping: %v %v", targets, ok)
	}
	if _, ok := p.mapped("react"); ok {
		t.Fatal("unmapped specifier matched")
	}
	s := Settings{Projects: []Project{{Dir: ".", BaseURL: ".", Paths: map[string][]string{"@/*": {"src/*"}}}, {Dir: "web", BaseURL: "web"}, {Dir: "web/admin", BaseURL: "web/admin"}, {Dir: "web/widgets", OutDir: "web/widgets/build"}}}
	if s.project("web/admin/src/a.ts", hasBaseURL).Dir != "web/admin" || s.project("web/src/a.ts", hasBaseURL).Dir != "web" || s.project("api/x.ts", hasBaseURL).Dir != "." || s.project("website/x.ts", hasBaseURL).Dir != "." || s.project("web/widgets/x.ts", hasBaseURL).Dir != "web" {
		t.Fatal("nearest base URL selection")
	}
	// Mappings stored once on the root apply to every descendant project.
	if s.project("web/admin/src/a.ts", hasPaths).Dir != "." || s.project("web/widgets/x.ts", hasPaths).Dir != "." {
		t.Fatal("nearest path mapping selection")
	}
}
