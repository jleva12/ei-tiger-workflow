package authorization

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net"
	"net/http"
	"strings"
	"time"

	"gorm.io/gorm"
)

const LocalDevelopmentIssuer = "urn:codegraph:local-development"
const LocalSiteAdminRole = "role:site-admin"
const localSiteAdminPermissions = "permgroup:site-admin"

// LocalDevelopmentProvider is opt-in and only accepts localhost or the three
// internal Compose service hosts. It is never the corporate authentication stub.
// Bind development services to loopback; every local request uses this identity.
// User names the local site administrator (LocalAdminUser when empty) and
// DisplayName labels them in the user registry.
type LocalDevelopmentProvider struct {
	TenantID    string
	User        string
	DisplayName string
}

// AdminUser is the configured local site administrator's username.
func (p LocalDevelopmentProvider) AdminUser() string {
	if p.User == "" {
		return LocalAdminUser
	}
	return p.User
}

func (p LocalDevelopmentProvider) Identity() (Identity, error) {
	if !identifier.MatchString(p.TenantID) || !identifier.MatchString(p.AdminUser()) || len(p.DisplayName) > 200 {
		return Identity{}, ErrUnavailable
	}
	now := time.Now()
	group := LocalDevelopmentGroup()
	user := p.AdminUser()
	name := p.DisplayName
	if name == "" {
		name = "Local administrator"
		if user != LocalAdminUser {
			name = localDisplayName(user)
		}
	}
	return Identity{
		SubjectID: SubjectKey(LocalDevelopmentIssuer, user),
		Issuer:    LocalDevelopmentIssuer, TenantID: p.TenantID,
		DisplayName: name, Email: user + "@localhost",
		GroupKeys:         []string{GroupKey(group.Issuer, group.CorporateTenant, group.ExternalID)},
		MembershipVersion: "local-development-v1", MembershipComplete: true,
		MembershipValidTo: now.Add(time.Minute), AuthenticatedUntil: now.Add(time.Hour),
		SiteAdministrator: true,
	}, nil
}

// LocalAdminUser is the default local identity. LocalUserCookie selects another
// local person, with no roles or site authority, to exercise team membership.
const (
	LocalAdminUser  = "local-admin"
	LocalUserCookie = "codegraph_local_user"
)

// LocalUser is a plain local person: verified issuer, no corporate groups.
func (p LocalDevelopmentProvider) LocalUser(externalID string) (Identity, error) {
	if !identifier.MatchString(p.TenantID) {
		return Identity{}, ErrUnavailable
	}
	if externalID == p.AdminUser() {
		return p.Identity()
	}
	if !identifier.MatchString(externalID) {
		return Identity{}, ErrUnauthenticated
	}
	now := time.Now()
	return Identity{
		SubjectID: SubjectKey(LocalDevelopmentIssuer, externalID),
		Issuer:    LocalDevelopmentIssuer, TenantID: p.TenantID,
		DisplayName: localDisplayName(externalID), Email: externalID + "@localhost",
		GroupKeys:         []string{},
		MembershipVersion: "local-development-v1", MembershipComplete: true,
		MembershipValidTo: now.Add(time.Minute), AuthenticatedUntil: now.Add(time.Hour),
	}, nil
}

func localDisplayName(externalID string) string {
	words := strings.FieldsFunc(externalID, func(r rune) bool { return r == '-' || r == '_' || r == '.' || r == ':' })
	for i, word := range words {
		words[i] = strings.ToUpper(word[:1]) + word[1:]
	}
	return strings.Join(words, " ")
}

func localDevelopmentHost(authority string) bool {
	host := authority
	if value, _, err := net.SplitHostPort(authority); err == nil {
		host = value
	}
	host = strings.Trim(strings.ToLower(host), "[]")
	return host == "localhost" || host == "127.0.0.1" || host == "::1" || host == "admin" || host == "api" || host == "mcp"
}

func (p LocalDevelopmentProvider) Authenticate(ctx context.Context, request *http.Request) (Identity, error) {
	if ctx.Err() != nil {
		return Identity{}, ErrUnavailable
	}
	if !localDevelopmentHost(request.Host) {
		return Identity{}, ErrUnauthenticated
	}
	if cookie, err := request.Cookie(LocalUserCookie); err == nil && cookie.Value != "" {
		return p.LocalUser(cookie.Value)
	}
	return p.Identity()
}

// VerifyUser accepts people in the explicit local namespace only, so a local
// administrator can add teammates who have not signed in yet.
func (p LocalDevelopmentProvider) VerifyUser(_ context.Context, tenant string, user User) (User, error) {
	if tenant != p.TenantID || !identifier.MatchString(tenant) || user.Issuer != LocalDevelopmentIssuer || !identifier.MatchString(user.ExternalID) {
		return User{}, ErrInvalid
	}
	if user.DisplayName == "" {
		user.DisplayName = localDisplayName(user.ExternalID)
	}
	if user.Email == "" {
		user.Email = user.ExternalID + "@localhost"
	}
	return user, nil
}

func LocalDevelopmentGroup() ExternalGroup {
	return ExternalGroup{Issuer: LocalDevelopmentIssuer, CorporateTenant: "local", ExternalID: "site-administrators", DisplayName: "Local site administrators"}
}

// Directory fixtures may be configured in the UI only in the explicit local
// namespace. Real corporate IDs still require the company Provider implementation.
func (p LocalDevelopmentProvider) VerifyExternalGroup(_ context.Context, tenant string, group ExternalGroup) (ExternalGroup, error) {
	if tenant != p.TenantID || !identifier.MatchString(tenant) || group.Issuer != LocalDevelopmentIssuer || group.CorporateTenant != "local" || !identifier.MatchString(group.ExternalID) {
		return ExternalGroup{}, ErrInvalid
	}
	if group.DisplayName == "" {
		group.DisplayName = group.ExternalID
	}
	return group, nil
}

// The local MCP service authenticates the same local identity itself. No user
// identity or credentials are serialized into outbound headers.
func (p LocalDevelopmentProvider) AuthorizeOutbound(ctx context.Context, request *http.Request) error {
	i, err := CurrentIdentity(ctx)
	if err != nil {
		return err
	}
	if i.Issuer != LocalDevelopmentIssuer || i.TenantID != p.TenantID || request.URL.Scheme != "http" || !localDevelopmentHost(request.URL.Host) || request.URL.Path != "/mcp" {
		return ErrForbidden
	}
	return nil
}

// SeedLocalAdministrator is an operator-only, atomic seed. The durable request
// marker makes startup retries no-ops, preserving later edits and revocations.
// The role has explicit tenant grants for the catalog, never an admin bypass.
func (s *Store) SeedLocalAdministrator(ctx context.Context, provider LocalDevelopmentProvider) (uint64, error) {
	identity, err := provider.Identity()
	if err != nil {
		return 0, err
	}
	group, err := provider.VerifyExternalGroup(ctx, identity.TenantID, LocalDevelopmentGroup())
	if err != nil {
		return 0, err
	}
	group.Key = GroupKey(group.Issuer, group.CorporateTenant, group.ExternalID)
	group.LastVerifiedAt = time.Now().UTC()
	const marker = "local-site-admin-v1"
	var committed uint64
	err = s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		r, err := revision(tx, true)
		if err != nil {
			return err
		}
		var prior requestRow
		err = tx.Table("authz_requests").Where("tenant_id=? AND request_key=?", identity.TenantID, marker).Take(&prior).Error
		if err == nil {
			if err = recordUser(tx, identity); err != nil {
				return err
			}
			committed, err = upgradeLocalSiteConfiguration(tx, identity, r.Revision)
			return err
		}
		if !errors.Is(err, gorm.ErrRecordNotFound) {
			return err
		}
		before, err := readConfiguration(tx, identity.TenantID)
		if err != nil {
			return err
		}
		for _, entry := range []struct{ table, key string }{{"authz_roles", LocalSiteAdminRole}, {"authz_permission_groups", localSiteAdminPermissions}} {
			var count int64
			if err = tx.Table(entry.table).Where("tenant_id=? AND `key`=?", identity.TenantID, entry.key).Count(&count).Error; err != nil {
				return err
			}
			if count != 0 {
				return fmt.Errorf("local seed key already exists: %w", ErrConflict)
			}
			now := time.Now().UTC()
			if err = tx.Table(entry.table).Create(&Named{identity.TenantID, entry.key, "Site administrator", "All application permissions for local development", now, now}).Error; err != nil {
				return err
			}
		}
		grants := make([]Grant, 0, len(catalog))
		for _, permission := range catalog {
			grants = append(grants, Grant{permission.Kind, permission.Action, "tenant"})
		}
		for _, command := range []Command{
			{Operation: "group.grants", Key: localSiteAdminPermissions, Grants: grants},
			{Operation: "role.groups", Key: LocalSiteAdminRole, PermissionGroups: []string{localSiteAdminPermissions}},
			{Operation: "mapping.add", Role: LocalSiteAdminRole, ExternalGroup: group},
		} {
			if err = applyCommand(tx, identity.TenantID, command); err != nil {
				return err
			}
		}
		after, err := readConfiguration(tx, identity.TenantID)
		if err != nil {
			return err
		}
		if err = validateConfiguration(after, true); err != nil {
			return err
		}
		committed = r.Revision + 1
		after.Revision = committed
		if err = auditCommit(tx, identity.SubjectID, identity.TenantID, RequestID(ctx), "local_admin_seed", before, after, committed); err != nil {
			return err
		}
		if err = tx.Table("authz_requests").Create(&requestRow{identity.TenantID, identity.SubjectID, marker, stableKey("", marker), committed}).Error; err != nil {
			return err
		}
		if err = tx.Table("authz_requests").Create(&requestRow{identity.TenantID, identity.SubjectID, "local-site-configuration-v3", stableKey("", "local-site-configuration-v3"), committed}).Error; err != nil {
			return err
		}
		if err = recordUser(tx, identity); err != nil {
			return err
		}
		return tx.Exec("INSERT IGNORE INTO authz_bootstrap (tenant_id,revision) VALUES (?,?)", identity.TenantID, committed).Error
	}, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	return committed, err
}
