package authorization

import (
	"context"
	"errors"
	"testing"
)

func TestMySQLEditorSavesMetadataAndAccessAtomically(t *testing.T) {
	store, _ := mysqlStore(t)
	service, identity := bootstrapped(t, store)
	ctx, err := service.Pin(context.Background(), identity)
	if err != nil {
		t.Fatal(err)
	}
	revision, _ := store.Revision(ctx)
	apply := func(cmd Command) {
		t.Helper()
		cmd.ExpectedRevision = revision
		next, err := store.Apply(ctx, verifiedProvider{}, cmd)
		if err != nil {
			t.Fatal(err)
		}
		if next != revision+1 {
			t.Fatalf("expected one commit, got %d -> %d", revision, next)
		}
		revision = next
	}
	grants := []Grant{{"organization", "read", "owner"}, {"organization", "read", "owner_draft"}}
	apply(Command{Operation: "group.create", Key: "permgroup:readers", Name: "Readers", Grants: grants})
	apply(Command{Operation: "role.create", Key: "role:reader", Name: "Reader", PermissionGroups: []string{"permgroup:readers"}})
	state, err := store.Configuration(ctx, identity.TenantID)
	if err != nil {
		t.Fatal(err)
	}
	count := func(kind, key string) int {
		n := 0
		for _, rule := range state.Rules {
			if rule.Type == kind && rule.Values[0] == key {
				n++
			}
		}
		return n
	}
	if count("p", "permgroup:readers") != 2 || count("g", "role:reader") != 1 {
		t.Fatalf("missing assignments: %+v", state.Rules)
	}
	// Metadata-only saves leave access untouched.
	apply(Command{Operation: "group.update", Key: "permgroup:readers", Name: "Code readers", IdempotencyKey: "rename-readers"})
	// Empty and omitted access lists must have different retry fingerprints.
	if _, err := store.Apply(ctx, verifiedProvider{}, Command{Operation: "group.update", Key: "permgroup:readers", Name: "Code readers", Grants: []Grant{}, ExpectedRevision: revision - 1, IdempotencyKey: "rename-readers"}); !errors.Is(err, ErrConflict) {
		t.Fatalf("empty access list reused metadata-only retry: %v", err)
	}
	state, _ = store.Configuration(ctx, identity.TenantID)
	if count("p", "permgroup:readers") != 2 {
		t.Fatal("metadata save cleared access")
	}
	// Invalid assignments roll back the metadata, revision, and audit event.
	for _, cmd := range []Command{
		{Operation: "role.create", Key: "role:invalid", Name: "Invalid", PermissionGroups: []string{"permgroup:missing"}},
		{Operation: "group.create", Key: "permgroup:invalid", Name: "Invalid", Grants: []Grant{{"organization", "read", "invalid"}}},
		{Operation: "group.update", Key: "permgroup:readers", Name: "Should roll back", Grants: []Grant{{"organization", "read", "invalid"}}},
	} {
		cmd.ExpectedRevision = revision
		if _, err := store.Apply(ctx, verifiedProvider{}, cmd); !errors.Is(err, ErrInvalid) {
			t.Fatalf("expected invalid, got %v", err)
		}
	}
	state, _ = store.Configuration(ctx, identity.TenantID)
	if state.Revision != revision || len(state.Roles) != 2 || len(state.PermissionGroups) != 2 || count("p", "permgroup:readers") != 2 {
		t.Fatalf("partial commit: %+v", state)
	}
	for _, group := range state.PermissionGroups {
		if group.Key == "permgroup:readers" && group.Name != "Code readers" {
			t.Fatal("metadata was not rolled back")
		}
	}
	history, _ := store.Audit(ctx, identity.TenantID, 0)
	if len(history) != 4 {
		t.Fatalf("expected bootstrap and 3 editor commits, got %d", len(history))
	}
	// Explicit empty lists remove assignments, including when sent with metadata.
	apply(Command{Operation: "role.update", Key: "role:reader", Name: "Reader", PermissionGroups: []string{}})
	apply(Command{Operation: "group.update", Key: "permgroup:readers", Name: "Code readers", Grants: []Grant{}})
	state, _ = store.Configuration(ctx, identity.TenantID)
	if count("p", "permgroup:readers") != 0 || count("g", "role:reader") != 0 {
		t.Fatal("explicit clear did not remove access")
	}
}
