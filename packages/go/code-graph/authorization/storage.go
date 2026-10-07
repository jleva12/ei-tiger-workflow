package authorization

import (
	"context"
	"database/sql"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strings"
	"time"

	gormadapter "github.com/casbin/gorm-adapter/v3"
	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
	"gorm.io/gorm/logger"
)

//go:embed migrations/001_authorization.sql
var schema string

//go:embed migrations/002_membership.sql
var membershipSchema string

//go:embed migrations/003_user_integrations.sql
var integrationSchema string

type Store struct{ db *gorm.DB }

func OpenMySQL(dsn string) (*Store, error) {
	if dsn == "" {
		return nil, ErrUnavailable
	}
	db, err := gorm.Open(mysql.Open(dsn), &gorm.Config{Logger: logger.Default.LogMode(logger.Silent)})
	if err != nil {
		return nil, fmt.Errorf("authorization database connection failed: %w", err)
	}
	pool, err := db.DB()
	if err != nil {
		return nil, err
	}
	pool.SetMaxOpenConns(10)
	pool.SetMaxIdleConns(5)
	pool.SetConnMaxLifetime(5 * time.Minute)
	return &Store{db}, nil
}
func (s *Store) Close() error {
	db, err := s.db.DB()
	if err != nil {
		return err
	}
	return db.Close()
}

// Migrate is operator-only. Neither startup nor adapter construction runs DDL.
// Statements are restartable because MySQL DDL commits independently.
func (s *Store) Migrate(ctx context.Context) error {
	for _, statement := range strings.Split(schema+membershipSchema+integrationSchema, ";") {
		if strings.TrimSpace(statement) != "" {
			if err := s.db.WithContext(ctx).Exec(statement).Error; err != nil {
				return err
			}
		}
	}
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		for _, p := range catalog {
			b, _ := json.Marshal(p.Conditions)
			if err := tx.Exec("INSERT IGNORE INTO authz_permission_catalog (resource_kind,action,description,conditions,collection) VALUES (?,?,?,?,?)", p.Kind, p.Action, p.Description, string(b), p.Collection).Error; err != nil {
				return err
			}
		}
		return nil
	})
}

type revisionRow struct {
	ID           int
	Revision     uint64
	ModelVersion string
}

func revision(tx *gorm.DB, lock bool) (revisionRow, error) {
	var r revisionRow
	q := tx.Table("authz_revision")
	if lock {
		q = q.Clauses(clause.Locking{Strength: "UPDATE"})
	}
	err := q.Where("id = ?", 1).Take(&r).Error
	if err == nil && (r.ModelVersion != ModelVersion || r.Revision == 0) {
		err = ErrUnavailable
	}
	return r, err
}
func (s *Store) Revision(ctx context.Context) (uint64, error) {
	r, err := revision(s.db.WithContext(ctx), false)
	return r.Revision, err
}
func adapter(tx *gorm.DB) (*gormadapter.Adapter, error) {
	db := tx.Session(&gorm.Session{})
	gormadapter.TurnOffAutoMigrate(db)
	return gormadapter.NewAdapterByDB(db)
}
func loadSnapshot(tx *gorm.DB) (*Snapshot, error) {
	r, err := revision(tx, false)
	if err != nil {
		return nil, err
	}
	a, err := adapter(tx)
	if err != nil {
		return nil, err
	}
	e, err := enforcer(a)
	if err != nil {
		return nil, err
	}
	// The adapter is detached: published snapshots cannot write or reload.
	e.SetAdapter(nil)
	var tenants []string
	if err = tx.Table("authz_roles").Distinct("tenant_id").Pluck("tenant_id", &tenants).Error; err != nil {
		return nil, err
	}
	for _, tenant := range tenants {
		state, err := readConfiguration(tx, tenant)
		if err != nil {
			return nil, err
		}
		if err = validateConfiguration(state, false); err != nil {
			return nil, err
		}
	}
	// Validate every rule, including orphaned tenants absent from metadata.
	var rules []gormadapter.CasbinRule
	if err = tx.Table("casbin_rule").Find(&rules).Error; err != nil {
		return nil, err
	}
	for _, r := range rules {
		if r.Ptype != "p" && r.Ptype != "g" {
			return nil, ErrInvalid
		}
		tenant := r.V1
		if r.Ptype == "g" {
			tenant = r.V2
		}
		if !slices.Contains(tenants, tenant) {
			return nil, ErrInvalid
		}
	}
	var members []Membership
	if err = tx.Table("authz_team_members").Find(&members).Error; err != nil {
		return nil, err
	}
	if err = validateMemberships(members); err != nil {
		return nil, err
	}
	return &Snapshot{enforcer: e, members: membershipIndex(members), Revision: r.Revision, ModelVersion: ModelVersion, ConfirmedAt: time.Now()}, nil
}
func (s *Store) Load(ctx context.Context) (out *Snapshot, err error) {
	err = s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		// Check tables that an empty policy would otherwise never touch. Startup
		// must detect incomplete operator migrations without attempting DDL.
		for _, query := range []string{
			"SELECT tenant_id,`key`,name,description,created_at,updated_at FROM authz_roles LIMIT 0",
			"SELECT tenant_id,`key`,name,description,created_at,updated_at FROM authz_permission_groups LIMIT 0",
			"SELECT resource_kind,action,description,conditions,collection FROM authz_permission_catalog LIMIT 0",
			"SELECT group_key,issuer,corporate_tenant,external_id,display_name,last_verified_at FROM authz_external_groups LIMIT 0",
			"SELECT id,actor,tenant_id,request_id,operation,`before`,`after`,revision,created_at FROM authz_audit_events LIMIT 0",
			"SELECT id,revision,tenant_id,event_type,published_at,attempts,next_attempt_at FROM authz_outbox LIMIT 0",
			"SELECT tenant_id,actor,request_key,fingerprint,revision FROM authz_requests LIMIT 0",
			"SELECT tenant_id,revision FROM authz_bootstrap LIMIT 0",
			"SELECT subject_id,tenant_id,issuer,external_id,display_name,email,status,source,first_seen_at,last_seen_at FROM authz_users LIMIT 0",
			"SELECT tenant_id,team_id,subject_id,role,added_by,added_at FROM authz_team_members LIMIT 0",
			"SELECT tenant_id,subject_id,provider,account_id,account_login,status,credential,revision,connected_at,updated_at FROM authz_user_integrations LIMIT 0",
		} {
			rows, queryErr := tx.Raw(query).Rows()
			if queryErr != nil {
				return queryErr
			}
			if queryErr = rows.Close(); queryErr != nil {
				return queryErr
			}
		}
		out, err = loadSnapshot(tx)
		return err
	}, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	return
}

type Named struct {
	TenantID    string    `json:"-" gorm:"primaryKey"`
	Key         string    `json:"key" gorm:"primaryKey"`
	Name        string    `json:"name"`
	Description string    `json:"description"`
	CreatedAt   time.Time `json:"createdAt"`
	UpdatedAt   time.Time `json:"updatedAt"`
}
type Grant struct {
	Kind      string `json:"kind"`
	Action    string `json:"action"`
	Condition string `json:"condition"`
}
type Rule struct {
	Type   string   `json:"type"`
	Values []string `json:"values"`
}
type Configuration struct {
	Roles            []Named         `json:"roles"`
	PermissionGroups []Named         `json:"permissionGroups"`
	ExternalGroups   []ExternalGroup `json:"externalGroups"`
	Rules            []Rule          `json:"rules"`
	// TeamMembers lets deterministic snapshots carry memberships. SQL
	// snapshots read them from authz_team_members instead.
	TeamMembers []Membership `json:"teamMembers,omitempty"`
	Revision    uint64       `json:"policyRevision"`
}

func readConfiguration(tx *gorm.DB, tenant string) (Configuration, error) {
	out := Configuration{Roles: []Named{}, PermissionGroups: []Named{}, ExternalGroups: []ExternalGroup{}, Rules: []Rule{}}
	r, err := revision(tx, false)
	if err != nil {
		return out, err
	}
	out.Revision = r.Revision
	if err = tx.Table("authz_roles").Where("tenant_id = ?", tenant).Order("`key`").Find(&out.Roles).Error; err != nil {
		return out, err
	}
	if err = tx.Table("authz_permission_groups").Where("tenant_id = ?", tenant).Order("`key`").Find(&out.PermissionGroups).Error; err != nil {
		return out, err
	}
	var rules []gormadapter.CasbinRule
	if err = tx.Table("casbin_rule").Where("(ptype = 'p' AND v1 = ?) OR (ptype = 'g' AND v2 = ?)", tenant, tenant).Order("id").Find(&rules).Error; err != nil {
		return out, err
	}
	keys := []string{}
	for _, r := range rules {
		values := []string{r.V0, r.V1, r.V2}
		if r.Ptype == "p" {
			values = append(values, r.V3, r.V4)
			if r.V5 != "" {
				return out, ErrInvalid
			}
		} else if r.V3 != "" || r.V4 != "" || r.V5 != "" {
			return out, ErrInvalid
		}
		out.Rules = append(out.Rules, Rule{r.Ptype, values})
		if strings.HasPrefix(r.V0, "group:") {
			keys = append(keys, r.V0)
		}
	}
	if len(keys) > 0 {
		err = tx.Table("authz_external_groups").Where("group_key IN ?", keys).Order("group_key").Find(&out.ExternalGroups).Error
	}
	return out, err
}
func (s *Store) Configuration(ctx context.Context, tenant string) (out Configuration, err error) {
	err = s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error { out, err = readConfiguration(tx, tenant); return err }, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	return
}

var identifier = regexp.MustCompile(`^[A-Za-z0-9_:.-]{1,100}$`)

func validKey(k, prefix string) bool {
	return len(k) > len(prefix) && strings.HasPrefix(k, prefix) && identifier.MatchString(k)
}
func validateConfiguration(c Configuration, protectAdmin bool) error {
	roles, groups, external := map[string]bool{}, map[string]bool{}, map[string]bool{}
	for _, r := range c.Roles {
		if !validKey(r.Key, "role:") {
			return ErrInvalid
		}
		roles[r.Key] = true
	}
	for _, g := range c.PermissionGroups {
		if !validKey(g.Key, "permgroup:") {
			return ErrInvalid
		}
		groups[g.Key] = true
	}
	for _, g := range c.ExternalGroups {
		if g.Key != GroupKey(g.Issuer, g.CorporateTenant, g.ExternalID) || g.LastVerifiedAt.IsZero() {
			return ErrInvalid
		}
		external[g.Key] = true
	}
	writeGroups, readGroups, roleGroups, mapped := map[string]bool{}, map[string]bool{}, map[string][]string{}, map[string]bool{}
	for _, r := range c.Rules {
		for _, value := range r.Values {
			if !identifier.MatchString(value) {
				return ErrInvalid
			}
		}
		switch r.Type {
		case "p":
			if len(r.Values) != 5 || !groups[r.Values[0]] {
				return ErrInvalid
			}
			v := r.Values
			p, ok := permission(v[2], v[3])
			if !ok || !slices.Contains(p.Conditions, v[4]) {
				return ErrInvalid
			}
			if v[2] == "authorization_config" && v[4] == "tenant" {
				if v[3] == "write" {
					writeGroups[v[0]] = true
				}
				if v[3] == "read" {
					readGroups[v[0]] = true
				}
			}
		case "g":
			if len(r.Values) != 3 {
				return ErrInvalid
			}
			a, b := r.Values[0], r.Values[1]
			if external[a] && roles[b] {
				mapped[b] = true
			} else if roles[a] && groups[b] {
				roleGroups[a] = append(roleGroups[a], b)
			} else {
				return ErrInvalid
			}
		default:
			return ErrInvalid
		}
	}
	if protectAdmin {
		for role := range mapped {
			read, write := false, false
			for _, g := range roleGroups[role] {
				read = read || readGroups[g]
				write = write || writeGroups[g]
			}
			if read && write {
				return nil
			}
		}
		return fmt.Errorf("%w: final administrator mapping", ErrInvalid)
	}
	return nil
}

type Command struct {
	Operation        string        `json:"-"`
	Key              string        `json:"key,omitempty"`
	Name             string        `json:"name,omitempty"`
	Description      string        `json:"description,omitempty"`
	PermissionGroups []string      `json:"permissionGroups"`
	Grants           []Grant       `json:"grants"`
	ExternalGroup    ExternalGroup `json:"externalGroup,omitempty"`
	Role             string        `json:"role,omitempty"`
	ExpectedRevision uint64        `json:"expectedRevision"`
	IdempotencyKey   string        `json:"idempotencyKey,omitempty"`
}
type Commit struct {
	PolicyRevision uint64 `json:"policyRevision"`
	Applied        bool   `json:"applied"`
}
type requestRow struct {
	TenantID, Actor, RequestKey, Fingerprint string
	Revision                                 uint64
}
type Audit struct {
	ID        uint64    `json:"id"`
	Actor     string    `json:"actor"`
	TenantID  string    `json:"tenantId"`
	RequestID string    `json:"requestId"`
	Operation string    `json:"operation"`
	Before    string    `json:"before"`
	After     string    `json:"after"`
	Revision  uint64    `json:"revision"`
	CreatedAt time.Time `json:"createdAt"`
}

func auditCommit(tx *gorm.DB, actor, tenant, requestID, operation string, before, after any, revision uint64) error {
	b, _ := json.Marshal(before)
	a, _ := json.Marshal(after)
	if err := tx.Table("authz_audit_events").Create(&Audit{Actor: actor, TenantID: tenant, RequestID: requestID, Operation: operation, Before: string(b), After: string(a), Revision: revision, CreatedAt: time.Now().UTC()}).Error; err != nil {
		return err
	}
	if err := tx.Table("authz_outbox").Create(map[string]any{"revision": revision, "tenant_id": tenant, "event_type": "authorization.changed", "next_attempt_at": time.Now().UTC()}).Error; err != nil {
		return err
	}
	return tx.Table("authz_revision").Where("id = 1").Update("revision", revision).Error
}
func (s *Store) Apply(ctx context.Context, provider Provider, cmd Command) (uint64, error) {
	x, err := operation(ctx)
	if err != nil {
		return 0, err
	}
	if cmd.IdempotencyKey != "" && !identifier.MatchString(cmd.IdempotencyKey) {
		return 0, ErrInvalid
	}
	if cmd.Operation == "mapping.add" {
		if provider == nil {
			return 0, ErrUnavailable
		}
		g, err := provider.VerifyExternalGroup(ctx, x.identity.TenantID, cmd.ExternalGroup)
		if err != nil {
			return 0, err
		}
		if g.Issuer == "" || g.CorporateTenant == "" || g.ExternalID == "" || g.Issuer != cmd.ExternalGroup.Issuer || g.CorporateTenant != cmd.ExternalGroup.CorporateTenant || g.ExternalID != cmd.ExternalGroup.ExternalID {
			return 0, ErrInvalid
		}
		g.Key = GroupKey(g.Issuer, g.CorporateTenant, g.ExternalID)
		g.LastVerifiedAt = time.Now().UTC()
		cmd.ExternalGroup = g
	}
	// Exclude verification timestamps from retry identity.
	fingerprintCommand := cmd
	fingerprintCommand.ExternalGroup.LastVerifiedAt = time.Time{}
	b, _ := json.Marshal(struct {
		Operation string
		Command   Command
	}{cmd.Operation, fingerprintCommand})
	fingerprint := strings.TrimPrefix(stableKey("", string(b)), "")
	var committed uint64
	err = s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		r, err := revision(tx, true)
		if err != nil {
			return err
		}
		if err = validateIdentity(x.identity, time.Now()); err != nil {
			return err
		}
		// Rebuild under the row lock; a revoked administrative grant cannot write.
		snapshot, err := loadSnapshot(tx)
		if err != nil {
			return err
		}
		p, err := resolve(snapshot, x.identity)
		if err != nil {
			return err
		}
		allowed, err := snapshot.enforcer.Enforce(p, p.TenantID, Resource{Kind: "authorization_config", TenantID: p.TenantID}, "write")
		if err != nil {
			return ErrUnavailable
		}
		if !allowed {
			return ErrForbidden
		}
		if cmd.IdempotencyKey != "" {
			var prior requestRow
			err := tx.Table("authz_requests").Where("tenant_id=? AND actor=? AND request_key=?", p.TenantID, p.SubjectID, cmd.IdempotencyKey).Take(&prior).Error
			if err == nil {
				if prior.Fingerprint != fingerprint {
					return ErrConflict
				}
				committed = prior.Revision
				return nil
			}
			if !errors.Is(err, gorm.ErrRecordNotFound) {
				return err
			}
		}
		if cmd.ExpectedRevision != r.Revision {
			return ErrConflict
		}
		before, err := readConfiguration(tx, p.TenantID)
		if err != nil {
			return err
		}
		if err = applyCommand(tx, p.TenantID, cmd); err != nil {
			return err
		}
		after, err := readConfiguration(tx, p.TenantID)
		if err != nil {
			return err
		}
		if err = validateConfiguration(after, true); err != nil {
			return err
		}
		committed = r.Revision + 1
		after.Revision = committed
		if err = auditCommit(tx, p.SubjectID, p.TenantID, RequestID(ctx), cmd.Operation, before, after, committed); err != nil {
			return err
		}
		if cmd.IdempotencyKey != "" {
			return tx.Table("authz_requests").Create(&requestRow{p.TenantID, p.SubjectID, cmd.IdempotencyKey, fingerprint, committed}).Error
		}
		return nil
	}, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	return committed, err
}
func applyCommand(tx *gorm.DB, tenant string, c Command) error {
	a, err := adapter(tx)
	if err != nil {
		return err
	}
	table, prefix := "authz_roles", "role:"
	if strings.HasPrefix(c.Operation, "group.") {
		table, prefix = "authz_permission_groups", "permgroup:"
	}
	if strings.HasPrefix(c.Operation, "role.") || strings.HasPrefix(c.Operation, "group.") {
		if !validKey(c.Key, prefix) {
			return ErrInvalid
		}
	}
	exists := func(table, key string) bool {
		var n int64
		return tx.Table(table).Where("tenant_id=? AND `key`=?", tenant, key).Count(&n).Error == nil && n == 1
	}
	switch c.Operation {
	case "role.create", "group.create", "role.update", "group.update":
		if strings.TrimSpace(c.Name) == "" || len(c.Name) > 200 || len(c.Description) > 4000 {
			return ErrInvalid
		}
		if strings.HasSuffix(c.Operation, "update") {
			if !exists(table, c.Key) {
				return ErrNotFound
			}
			if err = tx.Table(table).Where("tenant_id=? AND `key`=?", tenant, c.Key).Updates(map[string]any{"name": c.Name, "description": c.Description, "updated_at": time.Now().UTC()}).Error; err != nil {
				return err
			}
		} else {
			if exists(table, c.Key) {
				return ErrConflict
			}
			now := time.Now().UTC()
			if err = tx.Table(table).Create(&Named{tenant, c.Key, c.Name, c.Description, now, now}).Error; err != nil {
				return err
			}
		}
		// Optional assignments are saved in the same transaction as the details.
		// Omitted fields preserve existing access; an explicit empty list clears it.
		if prefix == "role:" && c.PermissionGroups != nil {
			return applyCommand(tx, tenant, Command{Operation: "role.groups", Key: c.Key, PermissionGroups: c.PermissionGroups})
		}
		if prefix == "permgroup:" && c.Grants != nil {
			return applyCommand(tx, tenant, Command{Operation: "group.grants", Key: c.Key, Grants: c.Grants})
		}
		return nil
	case "role.delete", "group.delete":
		if !exists(table, c.Key) {
			return ErrNotFound
		}
		if err = tx.Table(table).Where("tenant_id=? AND `key`=?", tenant, c.Key).Delete(&Named{}).Error; err != nil {
			return err
		}
		if err = a.RemoveFilteredPolicy("p", "p", 0, c.Key, tenant); err != nil {
			return err
		}
		if err = a.RemoveFilteredPolicy("g", "g", 0, c.Key, "", tenant); err != nil {
			return err
		}
		return a.RemoveFilteredPolicy("g", "g", 0, "", c.Key, tenant)
	case "role.groups":
		if !exists(table, c.Key) || len(c.PermissionGroups) > 500 {
			return ErrInvalid
		}
		if err = a.RemoveFilteredPolicy("g", "g", 0, c.Key, "", tenant); err != nil {
			return err
		}
		for _, key := range c.PermissionGroups {
			if !exists("authz_permission_groups", key) {
				return ErrInvalid
			}
			if err = a.AddPolicy("g", "g", []string{c.Key, key, tenant}); err != nil {
				return err
			}
		}
		return nil
	case "group.grants":
		if !exists(table, c.Key) || len(c.Grants) > 500 {
			return ErrInvalid
		}
		if err = a.RemoveFilteredPolicy("p", "p", 0, c.Key, tenant); err != nil {
			return err
		}
		for _, g := range c.Grants {
			p, ok := permission(g.Kind, g.Action)
			if !ok || !slices.Contains(p.Conditions, g.Condition) {
				return ErrInvalid
			}
			if err = a.AddPolicy("p", "p", []string{c.Key, tenant, g.Kind, g.Action, g.Condition}); err != nil {
				return err
			}
		}
		return nil
	case "mapping.add":
		if !exists("authz_roles", c.Role) {
			return ErrInvalid
		}
		g := c.ExternalGroup
		if g.Key != GroupKey(g.Issuer, g.CorporateTenant, g.ExternalID) || g.LastVerifiedAt.IsZero() {
			return ErrInvalid
		}
		if err = tx.Table("authz_external_groups").Clauses(clause.OnConflict{UpdateAll: true}).Create(&g).Error; err != nil {
			return err
		}
		return a.AddPolicy("g", "g", []string{g.Key, c.Role, tenant})
	case "mapping.delete":
		if !validKey(c.Key, "group:") || !validKey(c.Role, "role:") {
			return ErrInvalid
		}
		return a.RemovePolicy("g", "g", []string{c.Key, c.Role, tenant})
	default:
		return ErrInvalid
	}
}
func (s *Store) Audit(ctx context.Context, tenant string, before uint64) ([]Audit, error) {
	out := []Audit{}
	q := s.db.WithContext(ctx).Table("authz_audit_events").Where("tenant_id=?", tenant)
	if before > 0 {
		q = q.Where("revision < ?", before)
	}
	err := q.Order("revision DESC").Limit(50).Find(&out).Error
	return out, err
}

// Bootstrap must be explicitly called by an operator with a verified directory
// group. The durable marker prevents restart/retry from resurrecting grants.
func (s *Store) Bootstrap(ctx context.Context, provider Provider, tenant, actor string, group ExternalGroup, recoveryReason string) (uint64, error) {
	if provider == nil || !identifier.MatchString(tenant) || actor == "" {
		return 0, ErrInvalid
	}
	g, err := provider.VerifyExternalGroup(ctx, tenant, group)
	if err != nil {
		return 0, err
	}
	if g.Issuer != group.Issuer || g.CorporateTenant != group.CorporateTenant || g.ExternalID != group.ExternalID || g.ExternalID == "" {
		return 0, ErrInvalid
	}
	g.Key = GroupKey(g.Issuer, g.CorporateTenant, g.ExternalID)
	g.LastVerifiedAt = time.Now().UTC()
	var committed uint64
	err = s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		r, err := revision(tx, true)
		if err != nil {
			return err
		}
		var count int64
		if err = tx.Table("authz_bootstrap").Where("tenant_id=?", tenant).Count(&count).Error; err != nil {
			return err
		}
		if count > 0 && recoveryReason == "" {
			committed = r.Revision
			return nil
		}
		before, err := readConfiguration(tx, tenant)
		if err != nil {
			return err
		}
		now := time.Now().UTC()
		for _, entry := range []struct{ table, key string }{{"authz_roles", "role:authorization-admin"}, {"authz_permission_groups", "permgroup:authorization-admin"}} {
			if err = tx.Table(entry.table).Clauses(clause.OnConflict{DoNothing: true}).Create(&Named{tenant, entry.key, "Access administrator", "Manages application authorization", now, now}).Error; err != nil {
				return err
			}
		}
		if err = applyCommand(tx, tenant, Command{Operation: "group.grants", Key: "permgroup:authorization-admin", Grants: []Grant{{"authorization_config", "read", "tenant"}, {"authorization_config", "write", "tenant"}}}); err != nil {
			return err
		}
		a, err := adapter(tx)
		if err != nil {
			return err
		}
		if err = a.AddPolicy("g", "g", []string{"role:authorization-admin", "permgroup:authorization-admin", tenant}); err != nil {
			return err
		}
		if err = applyCommand(tx, tenant, Command{Operation: "mapping.add", Role: "role:authorization-admin", ExternalGroup: g}); err != nil {
			return err
		}
		after, err := readConfiguration(tx, tenant)
		if err != nil {
			return err
		}
		if err = validateConfiguration(after, true); err != nil {
			return err
		}
		committed = r.Revision + 1
		op := "bootstrap"
		if recoveryReason != "" {
			op = "operator_recovery"
		}
		after.Revision = committed
		if err = auditCommit(tx, actor, tenant, RequestID(ctx), op, before, map[string]any{"configuration": after, "reason": recoveryReason}, committed); err != nil {
			return err
		}
		return tx.Exec("INSERT IGNORE INTO authz_bootstrap (tenant_id,revision) VALUES (?,?)", tenant, committed).Error
	})
	return committed, err
}
