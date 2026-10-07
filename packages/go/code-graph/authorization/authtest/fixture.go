// Package authtest supplies explicit verified identities for integration tests.
// Never use this provider in a running application.
package authtest

import (
	"context"
	"net/http"
	"testing"
	"time"

	"ei-aitiger-codegraph/authorization"
)

type Provider struct{ Identity authorization.Identity }

func (p Provider) Authenticate(context.Context, *http.Request) (authorization.Identity, error) {
	return p.Identity, nil
}
func (p Provider) VerifyExternalGroup(_ context.Context, _ string, g authorization.ExternalGroup) (authorization.ExternalGroup, error) {
	return g, nil
}

type Store struct{ Snapshot *authorization.Snapshot }

func (s Store) Revision(context.Context) (uint64, error)              { return s.Snapshot.Revision, nil }
func (s Store) Load(context.Context) (*authorization.Snapshot, error) { return s.Snapshot, nil }
func Service(t testing.TB, tenant string) (*authorization.Service, Provider) {
	t.Helper()
	return ServiceWithMembers(t, tenant, nil)
}

// ServiceWithMembers builds the fixture with team memberships in its snapshot.
func ServiceWithMembers(t testing.TB, tenant string, members []authorization.Membership) (*authorization.Service, Provider) {
	t.Helper()
	group := authorization.ExternalGroup{Issuer: "https://test.invalid", CorporateTenant: "corp", ExternalID: "test-group", LastVerifiedAt: time.Now()}
	group.Key = authorization.GroupKey(group.Issuer, group.CorporateTenant, group.ExternalID)
	c := authorization.Configuration{Revision: 1, Roles: []authorization.Named{{Key: "role:test"}}, PermissionGroups: []authorization.Named{{Key: "permgroup:test"}}, ExternalGroups: []authorization.ExternalGroup{group}, Rules: []authorization.Rule{{Type: "g", Values: []string{group.Key, "role:test", tenant}}, {Type: "g", Values: []string{"role:test", "permgroup:test", tenant}}}}
	for _, p := range authorization.Catalog() {
		c.Rules = append(c.Rules, authorization.Rule{Type: "p", Values: []string{"permgroup:test", tenant, p.Kind, p.Action, "tenant"}})
	}
	c.TeamMembers = members
	snapshot, err := authorization.BuildSnapshot(c)
	if err != nil {
		t.Fatal(err)
	}
	service, err := authorization.NewService(Store{snapshot}, authorization.DefaultOptions(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = service.Refresh(context.Background()); err != nil {
		t.Fatal(err)
	}
	provider := Provider{authorization.Identity{SubjectID: authorization.SubjectKey(group.Issuer, "test-user"), Issuer: group.Issuer, TenantID: tenant, GroupKeys: []string{group.Key}, MembershipVersion: "1", MembershipComplete: true, MembershipValidTo: time.Now().Add(time.Hour), AuthenticatedUntil: time.Now().Add(time.Hour)}}
	return service, provider
}
