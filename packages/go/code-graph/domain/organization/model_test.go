package organization

import (
	"errors"
	"slices"
	"testing"
	"time"
)

func fixture() []Entity {
	return []Entity{
		{ID: "usp", TenantID: "usp", Kind: Tenant, Name: "USP", Slug: "usp", Status: Active, Revision: 1},
		{ID: "cirrus", TenantID: "usp", PlatformID: "cirrus", ParentID: "usp", Kind: Platform, Name: "Cirrus", Slug: "cirrus", Status: Active, Revision: 1},
		{ID: "other", TenantID: "usp", PlatformID: "other", ParentID: "usp", Kind: Platform, Name: "Other", Slug: "other", Status: Active, Revision: 1},
		{ID: "core", TenantID: "usp", PlatformID: "cirrus", ParentID: "cirrus", Kind: Team, Name: "Core", Slug: "core", Status: Active, Revision: 1},
		{ID: "web", TenantID: "usp", PlatformID: "cirrus", ParentID: "cirrus", Kind: Team, Name: "Web", Slug: "web", Status: Active, Revision: 1},
		{ID: "outsider", TenantID: "usp", PlatformID: "other", ParentID: "other", Kind: Team, Name: "Other team", Slug: "other", Status: Active, Revision: 1},
		{ID: "repo", TenantID: "usp", PlatformID: "cirrus", ParentID: "cirrus", Kind: Repository, Name: "Shared", Slug: "shared", GraphRepositoryID: "graph:repo", RemoteURL: "https://github.com/example/shared", Status: Active, Revision: 1},
		{ID: "accounts", TenantID: "usp", PlatformID: "cirrus", ParentID: "core", Kind: Responsibility, Name: "Accounts", Slug: "accounts", RepositoryID: "repo", ScopeKind: Paths, Paths: []string{"services/accounts/"}, Status: Active, Revision: 1},
		{ID: "account-ui", TenantID: "usp", PlatformID: "cirrus", ParentID: "web", Kind: Responsibility, Name: "Account UI", Slug: "account-ui", RepositoryID: "repo", ScopeKind: Paths, Paths: []string{"services/accounts/ui/", "services/accounts-old/"}, Status: Active, Revision: 1},
	}
}

func TestScopeUnionAndEmptySelection(t *testing.T) {
	records := fixture()
	q := ScopeRequest{TenantID: "usp", PlatformID: "cirrus", Mode: SelectedTeams, TeamIDs: []string{"core", "web"}}
	scope, err := Resolve(records, q)
	if err != nil {
		t.Fatal(err)
	}
	if len(scope.Repositories) != 1 || !slices.Equal(scope.Repositories[0].Paths, []string{"services/accounts-old/", "services/accounts/"}) || len(scope.Repositories[0].Contributors) != 2 {
		t.Fatalf("bad deduplicated scope: %+v", scope)
	}
	q.TeamIDs = nil
	scope, err = Resolve(records, q)
	if err != nil || len(scope.Repositories) != 0 {
		t.Fatalf("empty selection broadened: %+v %v", scope, err)
	}
	q.Mode = EntirePlatform
	scope, err = Resolve(records, q)
	if err != nil || len(scope.Repositories) != 1 || scope.Repositories[0].ScopeKind != WholeRepository {
		t.Fatalf("platform scope: %+v %v", scope, err)
	}
	records[7].ScopeKind = WholeRepository
	records[7].Paths = nil
	q.Mode = SelectedTeams
	q.TeamIDs = []string{"core", "web"}
	scope, err = Resolve(records, q)
	if err != nil || scope.Repositories[0].ScopeKind != WholeRepository || len(scope.Repositories[0].Paths) != 0 || len(scope.Repositories[0].Contributors) != 2 {
		t.Fatalf("whole scope provenance: %+v %v", scope, err)
	}
}

func TestCrossPlatformAndTenantReferences(t *testing.T) {
	records := fixture()
	for _, q := range []ScopeRequest{
		{TenantID: "usp", PlatformID: "cirrus", Mode: SelectedTeams, TeamIDs: []string{"outsider"}},
		{TenantID: "missing", PlatformID: "cirrus", Mode: EntirePlatform},
		{TenantID: "usp", PlatformID: "cirrus", Mode: SelectedTeams, TeamIDs: []string{"core", "core"}},
	} {
		if _, err := Resolve(records, q); !errors.Is(err, ErrInvalid) {
			t.Fatalf("accepted invalid scope %+v: %v", q, err)
		}
	}
	e := records[7]
	e.ID = "new"
	e.Revision = 0
	e.Slug = "new"
	e.ParentID = "outsider"
	if _, err := Prepare(records, e, true, time.Now()); !errors.Is(err, ErrInvalid) {
		t.Fatalf("cross-platform assignment: %v", err)
	}
	e = records[3]
	e.ParentID = "other"
	e.PlatformID = "other"
	if _, err := Prepare(records, e, false, time.Now()); !errors.Is(err, ErrInvalid) {
		t.Fatalf("allowed team reparent: %v", err)
	}
}

func TestPathValidation(t *testing.T) {
	for _, p := range []string{"", "/root", "../src", "src/../x", "src//x", "src\\x", "./src", "src/*", "src/\n", " C:/x", "src/./x", "src/[]"} {
		if ValidatePath(p) == nil {
			t.Errorf("accepted %q", p)
		}
	}
	for _, p := range []string{"src/", "src/a.go", "src/account service/", "README.md"} {
		if err := ValidatePath(p); err != nil {
			t.Errorf("rejected %q: %v", p, err)
		}
	}
	e := fixture()[7]
	e.Paths = nil
	if err := e.Validate(); !errors.Is(err, ErrInvalid) {
		t.Fatalf("empty paths accepted: %v", err)
	}
}

func TestConflictsAndArchiveDependencies(t *testing.T) {
	records := fixture()
	now := time.Now().UTC()
	e := records[3]
	e.Revision = 0
	if _, err := Prepare(records, e, false, now); !errors.Is(err, ErrConflict) {
		t.Fatalf("stale revision: %v", err)
	}
	e = records[7]
	e.ID = "duplicate"
	e.Revision = 0
	e.Slug = "duplicate"
	if _, err := Prepare(records, e, true, now); !errors.Is(err, ErrConflict) {
		t.Fatalf("duplicate assignment accepted: %v", err)
	}
	e = records[7]
	e.ID = "overlap"
	e.Revision = 0
	e.Slug = "overlap"
	e.Paths = []string{"services/accounts/new/"}
	if len(Overlaps(records, e)) == 0 {
		t.Fatal("overlap not identified")
	}
	if _, err := Prepare(records, e, false, now); !errors.Is(err, ErrConflict) {
		t.Fatalf("unacknowledged overlap accepted: %v", err)
	}
	if _, err := Prepare(records, e, true, now); err != nil {
		t.Fatalf("acknowledged overlap rejected: %v", err)
	}
	e = records[1]
	e.Status = Archived
	if _, err := Prepare(records, e, false, now); !errors.Is(err, ErrConflict) {
		t.Fatalf("platform with children archived: %v", err)
	}
	w := Entity{ID: "workspace", TenantID: "usp", PlatformID: "cirrus", ParentID: "cirrus", Kind: Workspace, Name: "Delivery", Slug: "delivery", Status: Active, ScopeMode: SelectedTeams, TeamIDs: []string{"core"}}
	w, err := Prepare(records, w, false, now)
	if err != nil {
		t.Fatal(err)
	}
	records = append(records, w)
	e = records[7]
	e.Status = Archived
	if _, err := Prepare(records, e, false, now); !errors.Is(err, ErrConflict) {
		t.Fatalf("workspace dependency ignored: %v", err)
	}
	w.TeamIDs = nil
	if _, err := Prepare(records, w, false, now); err != nil {
		t.Fatalf("planning workspace should allow empty code context: %v", err)
	}
	w.Status = Draft
	if _, err := Prepare(records, w, false, now); err != nil {
		t.Fatalf("draft should allow empty scope: %v", err)
	}
}
