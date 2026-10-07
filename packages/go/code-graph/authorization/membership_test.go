package authorization

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// membershipFixture grants the caller no organization policy at all, so every
// allowed decision below comes from team membership alone.
func membershipFixture(t testing.TB, members []Membership) *Service {
	t.Helper()
	g := ExternalGroup{Issuer: "issuer", CorporateTenant: "corp", ExternalID: "staff", LastVerifiedAt: time.Now()}
	g.Key = GroupKey(g.Issuer, g.CorporateTenant, g.ExternalID)
	c := Configuration{Revision: 1, Roles: []Named{{Key: "role:viewer"}}, PermissionGroups: []Named{{Key: "permgroup:viewer"}}, ExternalGroups: []ExternalGroup{g}, Rules: []Rule{{"g", []string{g.Key, "role:viewer", "tenant-a"}}, {"g", []string{"role:viewer", "permgroup:viewer", "tenant-a"}}, {"p", []string{"permgroup:viewer", "tenant-a", "operations", "read", "tenant"}}}, TeamMembers: members}
	snapshot, err := BuildSnapshot(c)
	if err != nil {
		t.Fatal(err)
	}
	service, err := NewService(&memoryStore{snapshot: snapshot, revision: 1}, DefaultOptions(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = service.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	return service
}
func person(name string) Identity {
	return Identity{SubjectID: SubjectKey("issuer", name), Issuer: "issuer", TenantID: "tenant-a", DisplayName: name, GroupKeys: []string{GroupKey("issuer", "corp", "staff")}, MembershipVersion: "1", MembershipComplete: true, MembershipValidTo: time.Now().Add(time.Minute), AuthenticatedUntil: time.Now().Add(time.Minute)}
}
func TestTeamMembershipDecisions(t *testing.T) {
	alice, bob, charlie := person("alice"), person("bob"), person("charlie")
	service := membershipFixture(t, []Membership{
		{TenantID: "tenant-a", TeamID: "team-1", SubjectID: alice.SubjectID, Role: LeadRole},
		{TenantID: "tenant-a", TeamID: "team-2", SubjectID: alice.SubjectID, Role: MemberRole},
		{TenantID: "tenant-b", TeamID: "team-3", SubjectID: alice.SubjectID, Role: LeadRole},
		{TenantID: "tenant-a", TeamID: "team-1", SubjectID: bob.SubjectID, Role: MemberRole},
	})
	ctx, err := service.Pin(context.Background(), alice)
	if err != nil {
		t.Fatal(err)
	}
	team := func(id, tenant string) Resource {
		return Resource{Kind: "organization", ID: id, TenantID: tenant, Team: id}
	}
	owned := func(teams ...string) Resource {
		return Resource{Kind: "organization", ID: "ws", TenantID: "tenant-a", Teams: teams}
	}
	for _, test := range []struct {
		name     string
		resource Resource
		action   string
		want     bool
	}{
		{"member reads own team", team("team-1", "tenant-a"), "read", true},
		{"member enters own team", team("team-1", "tenant-a"), "enter", true},
		{"lead manages own roster", team("team-1", "tenant-a"), "manage_members", true},
		{"member does not manage roster", team("team-2", "tenant-a"), "manage_members", false},
		{"membership never edits the team record", team("team-1", "tenant-a"), "update", false},
		{"other team is invisible", team("team-9", "tenant-a"), "read", false},
		{"explicit other organization membership", team("team-3", "tenant-b"), "read", true},
		{"explicit other organization entry", team("team-3", "tenant-b"), "enter", true},
		{"explicit other organization lead", team("team-3", "tenant-b"), "manage_members", true},
		{"same team ID in another tenant denied", team("team-1", "tenant-b"), "enter", false},
		{"membership does not follow team ID", team("team-3", "tenant-a"), "read", false},
		{"other organization owned record", Resource{Kind: "organization", ID: "ws", TenantID: "tenant-b", Teams: []string{"team-3"}}, "update", true},
		{"other organization ownership mismatch", Resource{Kind: "organization", ID: "ws", TenantID: "tenant-b", Teams: []string{"team-1"}}, "update", false},
		{"other organization policy denied", Resource{Kind: "authorization_config", TenantID: "tenant-b"}, "read", false},
		{"other organization repository denied", Resource{Kind: "repository", ID: "r", TenantID: "tenant-b", Teams: []string{"team-3"}}, "read", false},
		{"team-owned record readable", owned("team-1", "team-2"), "read", true},
		{"team-owned record editable", owned("team-1", "team-2"), "update", true},
		{"team-owned record archivable", owned("team-1"), "archive", true},
		{"team-owned record transitions", owned("team-2"), "transition", true},
		{"team-owned record is not a team", owned("team-1"), "enter", false},
		{"partial ownership denied", owned("team-1", "team-9"), "update", false},
		{"unowned record denied", Resource{Kind: "organization", ID: "x", TenantID: "tenant-a"}, "read", false},
		{"team workspace capability", Resource{Kind: "team_collection", TenantID: "tenant-a"}, "enter", true},
		{"repository never leaks", Resource{Kind: "repository", ID: "r", TenantID: "tenant-a", Teams: []string{"team-1"}}, "read", false},
	} {
		t.Run(test.name, func(t *testing.T) {
			d, err := Check(ctx, test.resource, test.action)
			if err != nil || d.Allowed != test.want {
				t.Fatalf("%+v: %+v %v", test.resource, d, err)
			}
			if test.want && d.ReasonCode != "team_membership" {
				t.Fatalf("reason %q", d.ReasonCode)
			}
		})
	}
	teams, err := Memberships(ctx)
	if err != nil || len(teams) != 3 || teams[0] != (TeamMembership{"tenant-a", "team-1", LeadRole}) || teams[1] != (TeamMembership{"tenant-a", "team-2", MemberRole}) || teams[2] != (TeamMembership{"tenant-b", "team-3", LeadRole}) {
		t.Fatalf("memberships %+v %v", teams, err)
	}
	if !MemberOf(ctx, "tenant-a", "team-1", "team-2") || MemberOf(ctx, "tenant-a", "team-1", "team-9") || MemberOf(ctx, "tenant-a") || !LeadsAnyTeam(ctx) {
		t.Fatal("membership helpers")
	}
	w := httptest.NewRecorder()
	service.Session(w, httptest.NewRequest("GET", "/v1/me/authorization", nil).WithContext(ctx))
	var session Session
	if err = json.Unmarshal(w.Body.Bytes(), &session); err != nil || len(session.Teams) != 3 || session.DisplayName != "alice" || !session.Capabilities["team_collection:enter"] || session.Capabilities["organization_collection:list"] {
		t.Fatalf("session %s %v", w.Body.String(), err)
	}
	bobContext, _ := service.Pin(context.Background(), bob)
	if LeadsAnyTeam(bobContext) || !MemberOf(bobContext, "tenant-a", "team-1") {
		t.Fatal("bob is a plain member")
	}
	charlieContext, _ := service.Pin(context.Background(), charlie)
	d, err := Check(charlieContext, Resource{Kind: "team_collection", TenantID: "tenant-a"}, "enter")
	if err != nil || d.Allowed {
		t.Fatal("no membership, no team workspace", d, err)
	}
	if _, err = Memberships(context.Background()); !errors.Is(err, ErrUnauthenticated) {
		t.Fatal(err)
	}
	if _, err = BuildSnapshot(Configuration{Revision: 1, TeamMembers: []Membership{{TenantID: "tenant-a", TeamID: "team-1", SubjectID: alice.SubjectID, Role: "owner"}}}); !errors.Is(err, ErrInvalid) {
		t.Fatal("invalid role accepted", err)
	}
}
func TestLocalDevelopmentUsers(t *testing.T) {
	provider := LocalDevelopmentProvider{TenantID: "tenant"}
	request := func(cookie string) *http.Request {
		r := httptest.NewRequest("GET", "http://localhost/v1/me/authorization", nil)
		if cookie != "" {
			r.AddCookie(&http.Cookie{Name: LocalUserCookie, Value: cookie})
		}
		return r
	}
	admin, err := provider.Authenticate(context.Background(), request(""))
	if err != nil || !admin.SiteAdministrator || admin.DisplayName != "Local administrator" || admin.Email != "local-admin@localhost" {
		t.Fatalf("%+v %v", admin, err)
	}
	same, err := provider.Authenticate(context.Background(), request(LocalAdminUser))
	if err != nil || same.SubjectID != admin.SubjectID {
		t.Fatal("explicit admin cookie", err)
	}
	alice, err := provider.Authenticate(context.Background(), request("alice.ames"))
	if err != nil || alice.SiteAdministrator || len(alice.GroupKeys) != 0 || alice.SubjectID != SubjectKey(LocalDevelopmentIssuer, "alice.ames") || alice.DisplayName != "Alice Ames" || alice.Email != "alice.ames@localhost" || alice.TenantID != "tenant" {
		t.Fatalf("%+v %v", alice, err)
	}
	if _, err = provider.Authenticate(context.Background(), request("not valid!")); !errors.Is(err, ErrUnauthenticated) {
		t.Fatal("invalid local user accepted", err)
	}
	user, err := provider.VerifyUser(context.Background(), "tenant", User{Issuer: LocalDevelopmentIssuer, ExternalID: "bob"})
	if err != nil || user.DisplayName != "Bob" || user.Email != "bob@localhost" {
		t.Fatalf("%+v %v", user, err)
	}
	for _, bad := range []User{{Issuer: "https://corporate.example", ExternalID: "bob"}, {Issuer: LocalDevelopmentIssuer, ExternalID: "bad user"}} {
		if _, err = provider.VerifyUser(context.Background(), "tenant", bad); !errors.Is(err, ErrInvalid) {
			t.Fatalf("%+v accepted: %v", bad, err)
		}
	}
	if _, err = provider.VerifyUser(context.Background(), "other", User{Issuer: LocalDevelopmentIssuer, ExternalID: "bob"}); !errors.Is(err, ErrInvalid) {
		t.Fatal("other tenant accepted")
	}
	named := LocalDevelopmentProvider{TenantID: "tenant", User: "jleva"}
	me, err := named.Authenticate(context.Background(), request(""))
	if err != nil || !me.SiteAdministrator || me.SubjectID != SubjectKey(LocalDevelopmentIssuer, "jleva") || me.DisplayName != "Jleva" || me.Email != "jleva@localhost" {
		t.Fatalf("configured administrator %+v %v", me, err)
	}
	if same, err := named.Authenticate(context.Background(), request("jleva")); err != nil || same.SubjectID != me.SubjectID || !same.SiteAdministrator {
		t.Fatal("configured administrator by cookie", err)
	}
	if other, err := named.Authenticate(context.Background(), request(LocalAdminUser)); err != nil || other.SiteAdministrator {
		t.Fatal("the default name is a plain person once another administrator is configured", err)
	}
	if labelled, err := (LocalDevelopmentProvider{TenantID: "tenant", User: "jleva", DisplayName: "Joseph Leva"}).Identity(); err != nil || labelled.DisplayName != "Joseph Leva" {
		t.Fatal("display name", labelled, err)
	}
	if _, err = (LocalDevelopmentProvider{TenantID: "tenant", User: "not valid"}).Identity(); !errors.Is(err, ErrUnavailable) {
		t.Fatal("invalid administrator name accepted", err)
	}
}
