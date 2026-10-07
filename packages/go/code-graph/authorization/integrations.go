package authorization

import (
	"context"
	"database/sql"
	"errors"
	"regexp"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

const (
	// IntegrationActive links a person to an account whose credential works.
	IntegrationActive = "active"
	// IntegrationExpired keeps the linked account on record after its
	// credential stopped working, so the person is asked to reconnect it.
	IntegrationExpired = "expired"
	maxCredentialBytes = 8192
)

var providerKey = regexp.MustCompile(`^[a-z][a-z0-9-]{0,31}$`)

// UserIntegration is one person's link to an external service. Credential is
// ciphertext sealed by the owning service; this store never interprets it.
// Revision orders credential rotations so concurrent refreshes cannot lose one.
type UserIntegration struct {
	TenantID     string `gorm:"primaryKey"`
	SubjectID    string `gorm:"primaryKey"`
	Provider     string `gorm:"primaryKey"`
	AccountID    string
	AccountLogin string
	Status       string
	Credential   []byte
	Revision     int64
	ConnectedAt  time.Time
	UpdatedAt    time.Time
}

func (u UserIntegration) validate() error {
	if !identifier.MatchString(u.TenantID) || !validKey(u.SubjectID, "user:") || !providerKey.MatchString(u.Provider) {
		return ErrInvalid
	}
	if u.AccountID == "" || len(u.AccountID) > 64 || u.AccountLogin == "" || len(u.AccountLogin) > 100 || len(u.Credential) == 0 || len(u.Credential) > maxCredentialBytes || (u.Status != IntegrationActive && u.Status != IntegrationExpired) {
		return ErrInvalid
	}
	return nil
}

// UserIntegration reads one person's link to a provider.
func (s *Store) UserIntegration(ctx context.Context, tenant, subject, provider string) (UserIntegration, error) {
	var out UserIntegration
	err := s.db.WithContext(ctx).Table("authz_user_integrations").Where("tenant_id=? AND subject_id=? AND provider=?", tenant, subject, provider).Take(&out).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return UserIntegration{}, ErrNotFound
	}
	return out, err
}

// ConnectUserIntegration records a newly authorized account, replacing any
// earlier link to the same provider and starting a new connection time.
func (s *Store) ConnectUserIntegration(ctx context.Context, in UserIntegration) (UserIntegration, error) {
	in.Status = IntegrationActive
	if err := in.validate(); err != nil {
		return UserIntegration{}, err
	}
	now := time.Now().UTC()
	in.ConnectedAt, in.UpdatedAt = now, now
	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var existing UserIntegration
		err := tx.Table("authz_user_integrations").Clauses(clause.Locking{Strength: "UPDATE"}).Where("tenant_id=? AND subject_id=? AND provider=?", in.TenantID, in.SubjectID, in.Provider).Take(&existing).Error
		if errors.Is(err, gorm.ErrRecordNotFound) {
			in.Revision = 1
			return tx.Table("authz_user_integrations").Create(&in).Error
		}
		if err != nil {
			return err
		}
		in.Revision = existing.Revision + 1
		return tx.Table("authz_user_integrations").Where("tenant_id=? AND subject_id=? AND provider=?", in.TenantID, in.SubjectID, in.Provider).Select("*").Updates(&in).Error
	}, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	if err != nil {
		return UserIntegration{}, err
	}
	return in, nil
}

// UpdateUserIntegration stores a rotated credential or status when the row is
// still at in.Revision. ErrConflict means another request rotated it first.
func (s *Store) UpdateUserIntegration(ctx context.Context, in UserIntegration) (UserIntegration, error) {
	if err := in.validate(); err != nil {
		return UserIntegration{}, err
	}
	in.UpdatedAt = time.Now().UTC()
	result := s.db.WithContext(ctx).Table("authz_user_integrations").
		Where("tenant_id=? AND subject_id=? AND provider=? AND revision=? AND account_id=?", in.TenantID, in.SubjectID, in.Provider, in.Revision, in.AccountID).
		Updates(map[string]any{"credential": in.Credential, "status": in.Status, "account_login": in.AccountLogin, "revision": in.Revision + 1, "updated_at": in.UpdatedAt})
	if result.Error != nil {
		return UserIntegration{}, result.Error
	}
	if result.RowsAffected == 0 {
		return UserIntegration{}, ErrConflict
	}
	in.Revision++
	return in, nil
}

// DeleteUserIntegration removes a person's link. Removing a missing link succeeds.
func (s *Store) DeleteUserIntegration(ctx context.Context, tenant, subject, provider string) error {
	return s.db.WithContext(ctx).Table("authz_user_integrations").Where("tenant_id=? AND subject_id=? AND provider=?", tenant, subject, provider).Delete(&UserIntegration{}).Error
}
