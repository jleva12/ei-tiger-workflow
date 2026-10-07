package authorization

import (
	"context"
	"database/sql"
	"encoding/base64"
	"errors"
	"regexp"
	"strings"
	"sync"
	"time"

	"gorm.io/gorm"
)

// SignInRecorder keeps the user registry current from verified identities. The
// SQL store implements it; deterministic test stores do not.
type SignInRecorder interface {
	RecordSignIn(context.Context, Identity) error
}

var teamIdentifier = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)

func validateMembership(m Membership) error {
	if !identifier.MatchString(m.TenantID) || !teamIdentifier.MatchString(m.TeamID) || !validKey(m.SubjectID, "user:") || (m.Role != MemberRole && m.Role != LeadRole) {
		return ErrInvalid
	}
	return nil
}
func validateMemberships(members []Membership) error {
	for _, m := range members {
		if err := validateMembership(m); err != nil {
			return err
		}
	}
	return nil
}

// Member is a roster entry joined with the user registry.
type Member struct {
	Membership
	DisplayName string `json:"displayName"`
	Email       string `json:"email"`
	Status      string `json:"status"`
}

// MembershipCommand changes one roster entry. Operation is member.add,
// member.remove or member.role; the team is taken from the request path.
type MembershipCommand struct {
	Operation        string `json:"-"`
	TenantID         string `json:"-"`
	TeamID           string `json:"-"`
	SubjectID        string `json:"subjectId,omitempty"`
	Role             string `json:"role,omitempty"`
	ExpectedRevision uint64 `json:"expectedRevision"`
}

// signIns throttles registry writes to one per subject per interval.
type signIns struct{ seen sync.Map }

const signInInterval = 10 * time.Minute

func (s *Service) recordSignIn(i Identity) {
	recorder, ok := s.store.(SignInRecorder)
	if !ok {
		return
	}
	now := time.Now()
	if last, seen := s.signIns.seen.Load(i.SubjectID); seen && now.Sub(last.(time.Time)) < signInInterval {
		return
	}
	s.signIns.seen.Store(i.SubjectID, now)
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := recorder.RecordSignIn(ctx, i); err != nil {
			s.signIns.seen.Delete(i.SubjectID)
			s.logger.Warn("user registry update failed", "error", err)
		}
	}()
}

func fallbackDisplayName(i Identity) string {
	if i.DisplayName != "" {
		return i.DisplayName
	}
	if local, _, ok := strings.Cut(i.Email, "@"); ok && local != "" {
		return local
	}
	if len(i.SubjectID) > 13 {
		return "user " + i.SubjectID[5:13]
	}
	return i.SubjectID
}

// RecordSignIn inserts a first-time user or refreshes an existing row's last
// seen time and directory attributes. Status and source are never downgraded.
func (s *Store) RecordSignIn(ctx context.Context, i Identity) error {
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error { return recordUser(tx, i) }, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
}

func recordUser(tx *gorm.DB, i Identity) error {
	if !validKey(i.SubjectID, "user:") || !identifier.MatchString(i.TenantID) || i.Issuer == "" {
		return ErrInvalid
	}
	now := time.Now().UTC()
	var existing User
	err := tx.Table("authz_users").Where("subject_id=?", i.SubjectID).Take(&existing).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return tx.Table("authz_users").Create(&User{SubjectID: i.SubjectID, TenantID: i.TenantID, Issuer: i.Issuer, DisplayName: fallbackDisplayName(i), Email: i.Email, Status: UserActive, Source: UserSignedIn, FirstSeenAt: now, LastSeenAt: now}).Error
	}
	if err != nil {
		return err
	}
	values := map[string]any{"last_seen_at": now}
	if i.DisplayName != "" {
		values["display_name"] = i.DisplayName
	}
	if i.Email != "" {
		values["email"] = i.Email
	}
	return tx.Table("authz_users").Where("subject_id=?", i.SubjectID).Updates(values).Error
}

func encodeUserCursor(u User) string {
	return base64.RawURLEncoding.EncodeToString([]byte(u.DisplayName + "\x1f" + u.SubjectID))
}
func decodeUserCursor(cursor string) (name, subject string, err error) {
	if cursor == "" {
		return "", "", nil
	}
	b, err := base64.RawURLEncoding.DecodeString(cursor)
	if err != nil {
		return "", "", ErrInvalid
	}
	name, subject, ok := strings.Cut(string(b), "\x1f")
	if !ok {
		return "", "", ErrInvalid
	}
	return name, subject, nil
}

// ListUsers pages the tenant's directory by display name, filtered by a
// case-insensitive substring of the name or email.
func (s *Store) ListUsers(ctx context.Context, tenant, query string, limit int, cursor string) ([]User, string, error) {
	if !identifier.MatchString(tenant) {
		return nil, "", ErrInvalid
	}
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	afterName, afterSubject, err := decodeUserCursor(cursor)
	if err != nil {
		return nil, "", err
	}
	q := s.db.WithContext(ctx).Table("authz_users").Where("tenant_id=?", tenant)
	if query = strings.TrimSpace(query); query != "" {
		if len(query) > 200 {
			return nil, "", ErrInvalid
		}
		pattern := "%" + strings.NewReplacer("\\", "\\\\", "%", "\\%", "_", "\\_").Replace(query) + "%"
		q = q.Where("(display_name LIKE ? OR email LIKE ?)", pattern, pattern)
	}
	if cursor != "" {
		q = q.Where("(display_name > ? OR (display_name = ? AND subject_id > ?))", afterName, afterName, afterSubject)
	}
	users := []User{}
	if err = q.Order("display_name").Order("subject_id").Limit(limit + 1).Find(&users).Error; err != nil {
		return nil, "", err
	}
	next := ""
	if len(users) > limit {
		users = users[:limit]
		next = encodeUserCursor(users[limit-1])
	}
	return users, next, nil
}

// User reads one registry row in the tenant.
func (s *Store) User(ctx context.Context, tenant, subject string) (User, error) {
	var out User
	err := s.db.WithContext(ctx).Table("authz_users").Where("tenant_id=? AND subject_id=?", tenant, subject).Take(&out).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return User{}, ErrNotFound
	}
	return out, err
}

// InviteUser records a directory-verified person before their first sign-in.
// An existing row keeps its status, source and first-seen time.
func (s *Store) InviteUser(ctx context.Context, provider Provider, tenant string, user User) (User, error) {
	directory, ok := provider.(UserDirectory)
	if !ok || !identifier.MatchString(tenant) {
		return User{}, ErrUnavailable
	}
	if user.Issuer == "" || user.ExternalID == "" || len(user.Issuer) > 512 || len(user.ExternalID) > 512 || len(user.DisplayName) > 200 || len(user.Email) > 320 {
		return User{}, ErrInvalid
	}
	verified, err := directory.VerifyUser(ctx, tenant, user)
	if err != nil {
		return User{}, err
	}
	if verified.Issuer != user.Issuer || verified.ExternalID != user.ExternalID || verified.DisplayName == "" || len(verified.DisplayName) > 200 || len(verified.Email) > 320 {
		return User{}, ErrInvalid
	}
	now := time.Now().UTC()
	out := User{SubjectID: SubjectKey(verified.Issuer, verified.ExternalID), TenantID: tenant, Issuer: verified.Issuer, ExternalID: verified.ExternalID, DisplayName: verified.DisplayName, Email: verified.Email, Status: UserActive, Source: UserInvited, FirstSeenAt: now, LastSeenAt: now}
	err = s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var existing User
		err := tx.Table("authz_users").Where("subject_id=?", out.SubjectID).Take(&existing).Error
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return tx.Table("authz_users").Create(&out).Error
		}
		if err != nil {
			return err
		}
		if existing.TenantID != tenant {
			return ErrConflict
		}
		out = existing
		return nil
	}, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	return out, err
}

func teamRoster(tx *gorm.DB, tenant, team string) ([]Member, error) {
	out := []Member{}
	err := tx.Table("authz_team_members AS m").
		Select("m.tenant_id, m.team_id, m.subject_id, m.role, m.added_by, m.added_at, COALESCE(u.display_name, '') AS display_name, COALESCE(u.email, '') AS email, COALESCE(u.status, '') AS status").
		Joins("LEFT JOIN authz_users u ON u.subject_id = m.subject_id").
		Where("m.tenant_id=? AND m.team_id=?", tenant, team).
		Order("display_name").Order("m.subject_id").Scan(&out).Error
	return out, err
}

// TeamMembers lists a team's roster with directory attributes.
func (s *Store) TeamMembers(ctx context.Context, tenant, team string) ([]Member, error) {
	if !identifier.MatchString(tenant) || !teamIdentifier.MatchString(team) {
		return nil, ErrInvalid
	}
	return teamRoster(s.db.WithContext(ctx), tenant, team)
}

// ApplyMembership changes a roster under the global revision lock. Authority
// comes from a lead membership of the team, a manage_members policy grant, or
// site configuration authority, all rechecked against committed state.
func (s *Store) ApplyMembership(ctx context.Context, cmd MembershipCommand) (uint64, error) {
	x, err := operation(ctx)
	if err != nil {
		return 0, err
	}
	if !identifier.MatchString(cmd.TenantID) || !teamIdentifier.MatchString(cmd.TeamID) || !validKey(cmd.SubjectID, "user:") {
		return 0, ErrInvalid
	}
	switch cmd.Operation {
	case "member.add", "member.role":
		if cmd.Role != MemberRole && cmd.Role != LeadRole {
			return 0, ErrInvalid
		}
	case "member.remove":
	default:
		return 0, ErrInvalid
	}
	var committed uint64
	err = s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		r, err := revision(tx, true)
		if err != nil {
			return err
		}
		if err = validateIdentity(x.identity, time.Now()); err != nil {
			return err
		}
		snapshot, err := loadSnapshot(tx)
		if err != nil {
			return err
		}
		p, err := resolve(snapshot, x.identity)
		if err != nil {
			return err
		}
		team := Resource{Kind: "organization", ID: cmd.TeamID, TenantID: cmd.TenantID, Team: cmd.TeamID}
		allowed := membershipAllows(p, team, "manage_members")
		if !allowed && cmd.TenantID == p.TenantID {
			allowed, err = snapshot.enforcer.Enforce(p, p.TenantID, team, "manage_members")
			if err != nil {
				return ErrUnavailable
			}
		}
		if !allowed && x.identity.SiteAdministrator {
			allowed, err = snapshot.enforcer.Enforce(p, p.TenantID, Resource{Kind: "site_configuration", TenantID: p.TenantID}, "write")
			if err != nil {
				return ErrUnavailable
			}
		}
		if !allowed {
			return ErrForbidden
		}
		if cmd.ExpectedRevision != r.Revision {
			return ErrConflict
		}
		before, err := teamRoster(tx, cmd.TenantID, cmd.TeamID)
		if err != nil {
			return err
		}
		switch cmd.Operation {
		case "member.add":
			var user User
			// People come from the actor's authorized sign-in directory, as in
			// ListUsers/InviteUser. The team may belong to another organization.
			if err = tx.Table("authz_users").Where("tenant_id=? AND subject_id=? AND status=?", p.TenantID, cmd.SubjectID, UserActive).Take(&user).Error; errors.Is(err, gorm.ErrRecordNotFound) {
				return ErrNotFound
			} else if err != nil {
				return err
			}
			for _, m := range before {
				if m.SubjectID == cmd.SubjectID {
					return ErrConflict
				}
			}
			if err = tx.Table("authz_team_members").Create(&Membership{TenantID: cmd.TenantID, TeamID: cmd.TeamID, SubjectID: cmd.SubjectID, Role: cmd.Role, AddedBy: p.SubjectID, AddedAt: time.Now().UTC()}).Error; err != nil {
				return err
			}
		case "member.remove":
			result := tx.Table("authz_team_members").Where("tenant_id=? AND team_id=? AND subject_id=?", cmd.TenantID, cmd.TeamID, cmd.SubjectID).Delete(&Membership{})
			if result.Error != nil {
				return result.Error
			}
			if result.RowsAffected == 0 {
				return ErrNotFound
			}
		case "member.role":
			result := tx.Table("authz_team_members").Where("tenant_id=? AND team_id=? AND subject_id=?", cmd.TenantID, cmd.TeamID, cmd.SubjectID).Update("role", cmd.Role)
			if result.Error != nil {
				return result.Error
			}
			if result.RowsAffected == 0 {
				var count int64
				if err = tx.Table("authz_team_members").Where("tenant_id=? AND team_id=? AND subject_id=?", cmd.TenantID, cmd.TeamID, cmd.SubjectID).Count(&count).Error; err != nil {
					return err
				}
				if count == 0 {
					return ErrNotFound
				}
			}
		}
		after, err := teamRoster(tx, cmd.TenantID, cmd.TeamID)
		if err != nil {
			return err
		}
		committed = r.Revision + 1
		return auditCommit(tx, p.SubjectID, cmd.TenantID, RequestID(ctx), cmd.Operation, map[string]any{"team": cmd.TeamID, "members": before}, map[string]any{"team": cmd.TeamID, "members": after}, committed)
	}, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	return committed, err
}
