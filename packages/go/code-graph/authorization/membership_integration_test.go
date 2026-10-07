package authorization

import (
	"context"
	"errors"
	"testing"
	"time"
)

type directoryProvider struct{ verifiedProvider }

func TestMySQLMembershipOutsideSignInDirectory(t *testing.T) {
	store, _ := mysqlStore(t)
	background := context.Background()
	provider := LocalDevelopmentProvider{TenantID: "directory"}
	revision, err := store.SeedLocalAdministrator(background, provider)
	if err != nil {
		t.Fatal(err)
	}
	service, err := NewService(store, DefaultOptions(), nil)
	if err != nil {
		t.Fatal(err)
	}
	pin := func(identity Identity) context.Context {
		t.Helper()
		if err := service.Refresh(background); err != nil {
			t.Fatal(err)
		}
		ctx, err := service.Pin(background, identity)
		if err != nil {
			t.Fatal(err)
		}
		return ctx
	}
	admin, _ := provider.Identity()
	ctx := pin(admin)
	alice, err := store.InviteUser(ctx, provider, "directory", User{Issuer: LocalDevelopmentIssuer, ExternalID: "alice"})
	if err != nil {
		t.Fatal(err)
	}
	bob, err := store.InviteUser(ctx, provider, "directory", User{Issuer: LocalDevelopmentIssuer, ExternalID: "bob"})
	if err != nil {
		t.Fatal(err)
	}
	cmd := MembershipCommand{Operation: "member.add", TenantID: "organization", TeamID: "team-1", SubjectID: alice.SubjectID, Role: LeadRole, ExpectedRevision: revision}
	revision, err = store.ApplyMembership(ctx, cmd)
	if err != nil {
		t.Fatal("add a directory user to a different organization", err)
	}
	aliceIdentity := admin
	aliceIdentity.SubjectID, aliceIdentity.GroupKeys, aliceIdentity.SiteAdministrator = alice.SubjectID, nil, false
	aliceContext := pin(aliceIdentity)
	team := Resource{Kind: "organization", ID: cmd.TeamID, Team: cmd.TeamID, TenantID: cmd.TenantID}
	for _, action := range []string{"read", "enter", "manage_members"} {
		if decision, err := Check(aliceContext, team, action); err != nil || !decision.Allowed {
			t.Fatalf("assigned lead %s: %+v %v", action, decision, err)
		}
	}
	memberships, err := Memberships(aliceContext)
	if err != nil || len(memberships) != 1 || memberships[0] != (TeamMembership{cmd.TenantID, cmd.TeamID, LeadRole}) {
		t.Fatal("session membership", memberships, err)
	}
	if decision, err := Check(aliceContext, Resource{Kind: "team_collection", TenantID: "directory"}, "enter"); err != nil || !decision.Allowed {
		t.Fatal("team route capability", decision, err)
	}
	// A lead uses the same directory when adding another person.
	cmd.SubjectID, cmd.Role, cmd.ExpectedRevision = bob.SubjectID, MemberRole, revision
	revision, err = store.ApplyMembership(aliceContext, cmd)
	if err != nil {
		t.Fatal("lead adds a directory member", err)
	}
	roster, err := store.TeamMembers(ctx, cmd.TenantID, cmd.TeamID)
	if err != nil || len(roster) != 2 || roster[0].DisplayName != "Alice" || roster[1].DisplayName != "Bob" {
		t.Fatal("roster retains directory names", roster, err)
	}
	// Directory boundaries still apply to people selected for a new membership.
	outsider := person("outsider")
	outsider.TenantID = "unrelated-directory"
	if err = store.RecordSignIn(background, outsider); err != nil {
		t.Fatal(err)
	}
	cmd.SubjectID, cmd.ExpectedRevision = outsider.SubjectID, revision
	if _, err = store.ApplyMembership(pin(admin), cmd); !errors.Is(err, ErrNotFound) {
		t.Fatal("unrelated directory user accepted", err)
	}
	if err = store.db.Table("authz_users").Where("subject_id=?", bob.SubjectID).Update("status", UserDisabled).Error; err != nil {
		t.Fatal(err)
	}
	cmd.TeamID, cmd.SubjectID = "team-2", bob.SubjectID
	if _, err = store.ApplyMembership(pin(admin), cmd); !errors.Is(err, ErrNotFound) {
		t.Fatal("disabled user accepted", err)
	}
	cmd.TeamID, cmd.SubjectID = "team-1", alice.SubjectID
	cmd.Operation = "member.remove"
	if _, err = store.ApplyMembership(pin(admin), cmd); err != nil {
		t.Fatal(err)
	}
	if decision, err := Check(pin(aliceIdentity), team, "enter"); err != nil || decision.Allowed {
		t.Fatal("removed member retained access", decision, err)
	}
	if _, err = store.ApplyMembership(pin(aliceIdentity), MembershipCommand{Operation: "member.role", TenantID: team.TenantID, TeamID: team.Team, SubjectID: bob.SubjectID, Role: LeadRole, ExpectedRevision: revision + 1}); !errors.Is(err, ErrForbidden) {
		t.Fatal("removed lead retained write authority", err)
	}
}

func (directoryProvider) VerifyUser(_ context.Context, _ string, u User) (User, error) {
	if u.DisplayName == "" {
		u.DisplayName = "Verified " + u.ExternalID
	}
	u.Email = u.ExternalID + "@example.com"
	return u, nil
}

func TestMySQLLocalAdministratorRename(t *testing.T) {
	store, _ := mysqlStore(t)
	ctx := context.Background()
	first := LocalDevelopmentProvider{TenantID: "tenant-a"}
	revision, err := store.SeedLocalAdministrator(ctx, first)
	if err != nil {
		t.Fatal(err)
	}
	// Renaming the local administrator reuses the seeded configuration and
	// records the new person; it never re-creates roles or grants.
	renamed := LocalDevelopmentProvider{TenantID: "tenant-a", User: "jleva"}
	again, err := store.SeedLocalAdministrator(ctx, renamed)
	if err != nil || again != revision {
		t.Fatal("rename re-seeded", again, revision, err)
	}
	identity, _ := renamed.Identity()
	user, err := store.User(ctx, "tenant-a", identity.SubjectID)
	if err != nil || user.DisplayName != "Jleva" || user.Status != UserActive {
		t.Fatalf("administrator registry row %+v %v", user, err)
	}
	service, err := NewService(store, DefaultOptions(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = service.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	execution, err := service.Pin(ctx, identity)
	if err != nil {
		t.Fatal(err)
	}
	if allowed, err := CanAdministerSite(execution, "write"); err != nil || !allowed {
		t.Fatal("renamed administrator lost site authority", allowed, err)
	}
}

func TestMySQLUserRegistryAndRosters(t *testing.T) {
	store, _ := mysqlStore(t)
	service, admin := bootstrapped(t, store)
	ctx, err := service.Pin(context.Background(), admin)
	if err != nil {
		t.Fatal(err)
	}
	revision, _ := store.Revision(ctx)
	grants := []Grant{{"authorization_config", "read", "tenant"}, {"authorization_config", "write", "tenant"}, {"organization", "read", "tenant"}, {"organization", "manage_members", "tenant"}}
	committed, err := store.Apply(ctx, verifiedProvider{}, Command{Operation: "group.grants", Key: "permgroup:authorization-admin", Grants: grants, ExpectedRevision: revision})
	if err != nil {
		t.Fatal(err)
	}
	// Sign-in recording is idempotent and refreshes directory attributes only.
	alice := Identity{SubjectID: SubjectKey("issuer", "alice"), Issuer: "issuer", TenantID: "tenant-a", DisplayName: "Alice Ames", Email: "alice@example.com", GroupKeys: []string{}, MembershipVersion: "1", MembershipComplete: true, MembershipValidTo: time.Now().Add(time.Hour), AuthenticatedUntil: time.Now().Add(time.Hour)}
	if err = store.RecordSignIn(ctx, alice); err != nil {
		t.Fatal(err)
	}
	first, err := store.User(ctx, "tenant-a", alice.SubjectID)
	if err != nil || first.Source != UserSignedIn || first.Status != UserActive || first.DisplayName != "Alice Ames" {
		t.Fatalf("%+v %v", first, err)
	}
	alice.DisplayName = "Alice A. Ames"
	if err = store.RecordSignIn(ctx, alice); err != nil {
		t.Fatal(err)
	}
	again, _ := store.User(ctx, "tenant-a", alice.SubjectID)
	if again.DisplayName != "Alice A. Ames" || !again.FirstSeenAt.Equal(first.FirstSeenAt) || again.LastSeenAt.Before(first.LastSeenAt) {
		t.Fatalf("sign-in refresh: %+v", again)
	}
	if _, err = store.InviteUser(ctx, verifiedProvider{}, "tenant-a", User{Issuer: "issuer", ExternalID: "bob"}); !errors.Is(err, ErrUnavailable) {
		t.Fatal("provider without a directory must not invent people", err)
	}
	bob, err := store.InviteUser(ctx, directoryProvider{}, "tenant-a", User{Issuer: "issuer", ExternalID: "bob"})
	if err != nil || bob.SubjectID != SubjectKey("issuer", "bob") || bob.Source != UserInvited || bob.DisplayName != "Verified bob" {
		t.Fatalf("%+v %v", bob, err)
	}
	if repeated, err := store.InviteUser(ctx, directoryProvider{}, "tenant-a", User{Issuer: "issuer", ExternalID: "bob", DisplayName: "Other"}); err != nil || repeated.DisplayName != bob.DisplayName {
		t.Fatal("re-invite must keep the existing row", repeated, err)
	}
	page, next, err := store.ListUsers(ctx, "tenant-a", "", 1, "")
	if err != nil || len(page) != 1 || next == "" || page[0].SubjectID != alice.SubjectID {
		t.Fatalf("first page %+v %q %v", page, next, err)
	}
	rest, more, err := store.ListUsers(ctx, "tenant-a", "", 1, next)
	if err != nil || len(rest) != 1 || more != "" || rest[0].SubjectID != bob.SubjectID {
		t.Fatalf("second page %+v %q %v", rest, more, err)
	}
	if found, _, err := store.ListUsers(ctx, "tenant-a", "ali", 10, ""); err != nil || len(found) != 1 {
		t.Fatalf("search %+v %v", found, err)
	}
	if found, _, err := store.ListUsers(ctx, "tenant-a", "%", 10, ""); err != nil || len(found) != 0 {
		t.Fatalf("wildcards are literal %+v %v", found, err)
	}
	// Roster writes serialize on the revision and require authority.
	if err = service.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	ctx, _ = service.Pin(context.Background(), admin)
	add := MembershipCommand{Operation: "member.add", TenantID: "tenant-a", TeamID: "team-1", SubjectID: alice.SubjectID, Role: LeadRole, ExpectedRevision: committed}
	committed, err = store.ApplyMembership(ctx, add)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = store.ApplyMembership(ctx, add); !errors.Is(err, ErrConflict) {
		t.Fatal("stale revision", err)
	}
	add.ExpectedRevision = committed
	if _, err = store.ApplyMembership(ctx, add); !errors.Is(err, ErrConflict) {
		t.Fatal("duplicate member", err)
	}
	add.SubjectID = SubjectKey("issuer", "nobody")
	if _, err = store.ApplyMembership(ctx, add); !errors.Is(err, ErrNotFound) {
		t.Fatal("unknown user", err)
	}
	// The snapshot carries the roster after reload.
	if err = service.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	aliceContext, err := service.Pin(context.Background(), alice)
	if err != nil {
		t.Fatal(err)
	}
	team := Resource{Kind: "organization", ID: "team-1", TenantID: "tenant-a", Team: "team-1"}
	if d, err := Check(aliceContext, team, "enter"); err != nil || !d.Allowed || d.ReasonCode != "team_membership" {
		t.Fatalf("member entry %+v %v", d, err)
	}
	if d, err := Check(aliceContext, Resource{Kind: "team_collection", TenantID: "tenant-a"}, "enter"); err != nil || !d.Allowed {
		t.Fatal("team workspace capability", d, err)
	}
	// A lead manages the roster; a plain member does not.
	committed, err = store.ApplyMembership(aliceContext, MembershipCommand{Operation: "member.add", TenantID: "tenant-a", TeamID: "team-1", SubjectID: bob.SubjectID, Role: MemberRole, ExpectedRevision: committed})
	if err != nil {
		t.Fatal("lead authority", err)
	}
	if err = service.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	bobIdentity := alice
	bobIdentity.SubjectID, bobIdentity.DisplayName = bob.SubjectID, bob.DisplayName
	bobContext, _ := service.Pin(context.Background(), bobIdentity)
	if _, err = store.ApplyMembership(bobContext, MembershipCommand{Operation: "member.role", TenantID: "tenant-a", TeamID: "team-1", SubjectID: bob.SubjectID, Role: LeadRole, ExpectedRevision: committed}); !errors.Is(err, ErrForbidden) {
		t.Fatal("member promoted themselves", err)
	}
	roster, err := store.TeamMembers(ctx, "tenant-a", "team-1")
	if err != nil || len(roster) != 2 || roster[0].DisplayName != "Alice A. Ames" || roster[1].Email != "bob@example.com" || roster[0].Role != LeadRole {
		t.Fatalf("roster %+v %v", roster, err)
	}
	aliceContext, _ = service.Pin(context.Background(), alice)
	committed, err = store.ApplyMembership(aliceContext, MembershipCommand{Operation: "member.role", TenantID: "tenant-a", TeamID: "team-1", SubjectID: bob.SubjectID, Role: LeadRole, ExpectedRevision: committed})
	if err != nil {
		t.Fatal(err)
	}
	committed, err = store.ApplyMembership(aliceContext, MembershipCommand{Operation: "member.remove", TenantID: "tenant-a", TeamID: "team-1", SubjectID: bob.SubjectID, ExpectedRevision: committed})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = store.ApplyMembership(aliceContext, MembershipCommand{Operation: "member.remove", TenantID: "tenant-a", TeamID: "team-1", SubjectID: bob.SubjectID, ExpectedRevision: committed}); !errors.Is(err, ErrNotFound) {
		t.Fatal("removed twice", err)
	}
	history, err := store.Audit(ctx, "tenant-a", 0)
	if err != nil || len(history) < 5 || history[0].Operation != "member.remove" {
		t.Fatalf("audit %+v %v", history, err)
	}
	var outbox int64
	store.db.Table("authz_outbox").Count(&outbox)
	if outbox != int64(len(history)) {
		t.Fatal("every roster change publishes", outbox, len(history))
	}
	if err = service.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	bobContext, _ = service.Pin(context.Background(), bobIdentity)
	if d, err := Check(bobContext, team, "read"); err != nil || d.Allowed {
		t.Fatal("removed member still reads the team", d, err)
	}
}
