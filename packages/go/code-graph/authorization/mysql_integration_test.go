package authorization

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"errors"
	"net/http"
	"os"
	"sync"
	"testing"
	"time"

	driver "github.com/go-sql-driver/mysql"
)

type verifiedProvider struct{}

func (verifiedProvider) Authenticate(context.Context, *http.Request) (Identity, error) {
	return Identity{}, ErrUnauthenticated
}
func (verifiedProvider) VerifyExternalGroup(_ context.Context, _ string, g ExternalGroup) (ExternalGroup, error) {
	g.DisplayName = "Operations"
	return g, nil
}

func mysqlStore(t *testing.T) (*Store, string) {
	t.Helper()
	dsn := os.Getenv("AUTHZ_TEST_MYSQL_DSN")
	if dsn == "" {
		t.Skip("set AUTHZ_TEST_MYSQL_DSN to a disposable MySQL 8 account with CREATE DATABASE")
	}
	config, err := driver.ParseDSN(dsn)
	if err != nil {
		t.Fatal(err)
	}
	config.ParseTime = true
	admin, err := sql.Open("mysql", config.FormatDSN())
	if err != nil {
		t.Fatal(err)
	}
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	name := "authz_it_" + hex.EncodeToString(b)
	if _, err = admin.Exec("CREATE DATABASE `" + name + "` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"); err != nil {
		t.Fatal(err)
	}
	config.DBName = name
	store, err := OpenMySQL(config.FormatDSN())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close(); _, _ = admin.Exec("DROP DATABASE `" + name + "`"); _ = admin.Close() })
	if err = store.Migrate(context.Background()); err != nil {
		t.Fatal(err)
	}
	return store, config.FormatDSN()
}
func bootstrapped(t *testing.T, store *Store) (*Service, Identity) {
	t.Helper()
	group := ExternalGroup{Issuer: "issuer", CorporateTenant: "corp", ExternalID: "admins"}
	if _, err := store.Bootstrap(context.Background(), verifiedProvider{}, "tenant-a", "operator", group, ""); err != nil {
		t.Fatal(err)
	}
	service, err := NewService(store, DefaultOptions(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = service.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	i := Identity{SubjectID: SubjectKey("issuer", "admin"), Issuer: "issuer", TenantID: "tenant-a", GroupKeys: []string{GroupKey("issuer", "corp", "admins")}, MembershipVersion: "1", MembershipComplete: true, MembershipValidTo: time.Now().Add(time.Hour), AuthenticatedUntil: time.Now().Add(time.Hour)}
	return service, i
}
func TestMySQLPersistenceAtomicityAndConflict(t *testing.T) {
	store, dsn := mysqlStore(t)
	service, i := bootstrapped(t, store)
	ctx, err := service.Pin(context.Background(), i)
	if err != nil {
		t.Fatal(err)
	}
	revision, _ := store.Revision(ctx)
	cmd := Command{Operation: "role.create", Key: "role:editor", Name: "Editor", ExpectedRevision: revision, IdempotencyKey: "create-editor"}
	committed, err := store.Apply(ctx, verifiedProvider{}, cmd)
	if err != nil {
		t.Fatal(err)
	}
	if repeated, err := store.Apply(ctx, verifiedProvider{}, cmd); err != nil || repeated != committed {
		t.Fatalf("idempotency: %d %v", repeated, err)
	}
	cmd.Key = "role:other"
	if _, err = store.Apply(ctx, verifiedProvider{}, cmd); !errors.Is(err, ErrConflict) {
		t.Fatal("idempotency mismatch", err)
	}
	cmd.IdempotencyKey = ""
	if _, err = store.Apply(ctx, verifiedProvider{}, cmd); !errors.Is(err, ErrConflict) {
		t.Fatal("stale revision", err)
	}
	cmd.ExpectedRevision = committed
	// Force the final outbox insertion to fail after metadata/rules/audit writes.
	if err = store.db.Exec("CREATE TRIGGER reject_outbox BEFORE INSERT ON authz_outbox FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='test rollback'").Error; err != nil {
		t.Fatal(err)
	}
	if _, err = store.Apply(ctx, verifiedProvider{}, cmd); err == nil {
		t.Fatal("expected rollback")
	}
	if err = store.db.Exec("DROP TRIGGER reject_outbox").Error; err != nil {
		t.Fatal(err)
	}
	state, err := store.Configuration(ctx, i.TenantID)
	if err != nil {
		t.Fatal(err)
	}
	if state.Revision != committed || len(state.Roles) != 2 {
		t.Fatalf("partial commit: %+v", state)
	}
	history, err := store.Audit(ctx, i.TenantID, 0)
	if err != nil || len(history) != 2 {
		t.Fatal("audit atomicity", history, err)
	}
	var outbox int64
	store.db.Table("authz_outbox").Count(&outbox)
	if outbox != 2 {
		t.Fatal("outbox atomicity", outbox)
	}
	// Two processes at one expected revision serialize; exactly one wins.
	var wg sync.WaitGroup
	results := make(chan error, 2)
	for _, key := range []string{"role:one", "role:two"} {
		wg.Add(1)
		go func(key string) {
			defer wg.Done()
			_, err := store.Apply(ctx, verifiedProvider{}, Command{Operation: "role.create", Key: key, Name: key, ExpectedRevision: committed})
			results <- err
		}(key)
	}
	wg.Wait()
	close(results)
	success, conflict := 0, 0
	for err := range results {
		if err == nil {
			success++
		} else if errors.Is(err, ErrConflict) {
			conflict++
		} else {
			t.Fatal(err)
		}
	}
	if success != 1 || conflict != 1 {
		t.Fatal(success, conflict)
	}
	second, err := OpenMySQL(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer second.Close()
	snapshot, err := second.Load(ctx)
	if err != nil || snapshot.Revision != committed+1 {
		t.Fatal("restart persistence", snapshot, err)
	}
}
func TestMySQLConfigurationSafetyAndPropagation(t *testing.T) {
	store, dsn := mysqlStore(t)
	first, i := bootstrapped(t, store)
	second, _ := NewService(store, DefaultOptions(), nil)
	if err := second.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	ctx, _ := first.Pin(context.Background(), i)
	revision, _ := store.Revision(ctx)
	for _, cmd := range []Command{
		{Operation: "role.delete", Key: "role:authorization-admin"},
		{Operation: "group.grants", Key: "permgroup:authorization-admin", Grants: []Grant{{"organization", "read", "bogus"}}},
		{Operation: "role.groups", Key: "role:authorization-admin", PermissionGroups: []string{"permgroup:missing"}},
		{Operation: "mapping.delete", Key: i.GroupKeys[0], Role: "role:authorization-admin"},
	} {
		cmd.ExpectedRevision = revision
		if _, err := store.Apply(ctx, verifiedProvider{}, cmd); !errors.Is(err, ErrInvalid) {
			t.Fatalf("unsafe command %+v: %v", cmd, err)
		}
	}
	grants := []Grant{{"authorization_config", "read", "tenant"}, {"authorization_config", "write", "tenant"}, {"organization", "read", "owner_draft"}}
	committed, err := store.Apply(ctx, verifiedProvider{}, Command{Operation: "group.grants", Key: "permgroup:authorization-admin", Grants: grants, ExpectedRevision: revision})
	if err != nil {
		t.Fatal(err)
	}
	// Missed event: polling alone repairs the second process.
	if second.AppliedRevision() == committed {
		t.Fatal("unrefreshed service changed")
	}
	if err = second.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	second.ObserveRevision(committed)
	second.ObserveRevision(committed - 1)
	ctx2, _ := second.Pin(context.Background(), i)
	allowed, err := Check(ctx2, Resource{Kind: "organization", TenantID: i.TenantID, OwnerID: i.SubjectID, Status: "draft"}, "read")
	if err != nil || !allowed.Allowed || allowed.PolicyRevision != committed {
		t.Fatal(allowed, err)
	}
	// Bootstrap is idempotent and does not overwrite deliberately edited grants.
	if _, err = store.Bootstrap(ctx, verifiedProvider{}, i.TenantID, "operator", ExternalGroup{Issuer: "issuer", CorporateTenant: "corp", ExternalID: "admins"}, ""); err != nil {
		t.Fatal(err)
	}
	state, _ := store.Configuration(ctx, i.TenantID)
	if state.Revision != committed {
		t.Fatal("bootstrap resurrected configuration")
	}
	// Removing a business grant survives a fresh connection and snapshot.
	committed, err = store.Apply(ctx, verifiedProvider{}, Command{Operation: "group.grants", Key: "permgroup:authorization-admin", Grants: grants[:2], ExpectedRevision: committed})
	if err != nil {
		t.Fatal(err)
	}
	restartedStore, err := OpenMySQL(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer restartedStore.Close()
	restarted, err := NewService(restartedStore, DefaultOptions(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = restarted.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	restartedContext, err := restarted.Pin(context.Background(), i)
	if err != nil {
		t.Fatal(err)
	}
	removed, err := Check(restartedContext, Resource{Kind: "organization", TenantID: i.TenantID, OwnerID: i.SubjectID, Status: "draft"}, "read")
	if err != nil || removed.Allowed {
		t.Fatal("removed grant returned after restart", removed, err)
	}
	// Simulate out-of-band corruption/revocation to verify the lock-bound recheck.
	if err = store.db.Exec("DELETE FROM casbin_rule WHERE ptype='p' AND v2='authorization_config' AND v3='write'").Error; err != nil {
		t.Fatal(err)
	}
	if _, err = store.Apply(ctx, verifiedProvider{}, Command{Operation: "role.create", Key: "role:revoked", Name: "Revoked", ExpectedRevision: committed}); !errors.Is(err, ErrForbidden) {
		t.Fatal("revoked administrator wrote", err)
	}
}
func TestMySQLScopeEquivalence(t *testing.T) {
	store, _ := mysqlStore(t)
	if err := store.db.Exec("CREATE TABLE resources (id INT PRIMARY KEY,tenant VARCHAR(100),owner VARCHAR(100),status VARCHAR(100))").Error; err != nil {
		t.Fatal(err)
	}
	resources := []Resource{}
	for _, tenant := range []string{"tenant-a", "tenant-b"} {
		for _, owner := range []string{"alice", "bob", ""} {
			for _, status := range []string{"draft", "submitted", "active", ""} {
				resources = append(resources, Resource{TenantID: tenant, OwnerID: owner, Status: status})
				if err := store.db.Exec("INSERT INTO resources VALUES (?,?,?,?)", len(resources), tenant, owner, status).Error; err != nil {
					t.Fatal(err)
				}
			}
		}
	}
	for _, templates := range [][]string{{}, {"tenant"}, {"owner"}, {"owner_draft"}, {"tenant_draft"}, {"tenant_submitted"}, {"owner_draft", "tenant_submitted"}, {"owner_draft", "tenant"}} {
		scope := Scope{TenantID: "tenant-a", SubjectID: "alice", Templates: templates}
		predicate, args, err := scope.SQL(Columns{"tenant", "owner", "status"})
		if err != nil {
			t.Fatal(err)
		}
		var ids []int
		if err = store.db.Raw("SELECT id FROM resources WHERE "+predicate, args).Scan(&ids).Error; err != nil {
			t.Fatal(err)
		}
		found := map[int]bool{}
		for _, id := range ids {
			found[id] = true
		}
		for idx, r := range resources {
			if found[idx+1] != scope.Allows(r) {
				t.Fatalf("%v %+v SQL=%v", templates, r, found[idx+1])
			}
		}
	}
}
