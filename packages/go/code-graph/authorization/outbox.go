package authorization

import (
	"context"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

type Event struct {
	ID        uint64 `json:"id"`
	Revision  uint64 `json:"revision"`
	TenantID  string `json:"tenantId"`
	EventType string `json:"type"`
	Attempts  int    `json:"-"`
}

// Publisher delivers at least once. Receivers must use ObserveRevision and
// Refresh and tolerate duplicates. Polling repairs missing events.
type Publisher interface {
	PublishAuthorizationChanged(context.Context, Event) error
}

func (s *Store) OutboxBacklog(ctx context.Context) (int64, error) {
	var count int64
	err := s.db.WithContext(ctx).Table("authz_outbox").Where("published_at IS NULL").Count(&count).Error
	return count, err
}

func (s *Store) Dispatch(ctx context.Context, publisher Publisher) error {
	if publisher == nil {
		return ErrUnavailable
	}
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var events []Event
		if err := tx.Table("authz_outbox").Clauses(clause.Locking{Strength: "UPDATE", Options: "SKIP LOCKED"}).Where("published_at IS NULL AND next_attempt_at <= ?", time.Now().UTC()).Order("id").Limit(20).Find(&events).Error; err != nil {
			return err
		}
		for _, event := range events {
			err := publisher.PublishAuthorizationChanged(ctx, event)
			values := map[string]any{"attempts": event.Attempts + 1}
			if err == nil {
				values["published_at"] = time.Now().UTC()
			} else {
				values["next_attempt_at"] = time.Now().UTC().Add(time.Duration(min(60, event.Attempts+1)) * time.Second)
			}
			if err = tx.Table("authz_outbox").Where("id=?", event.ID).Updates(values).Error; err != nil {
				return err
			}
		}
		return nil
	})
}
