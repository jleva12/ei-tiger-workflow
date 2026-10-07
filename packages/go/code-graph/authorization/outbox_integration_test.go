package authorization

import (
	"context"
	"errors"
	"testing"
	"time"
)

type publisherFunc func(context.Context, Event) error

func (p publisherFunc) PublishAuthorizationChanged(ctx context.Context, event Event) error {
	return p(ctx, event)
}

func TestMySQLOutboxRetryAndPolling(t *testing.T) {
	store, _ := mysqlStore(t)
	first, identity := bootstrapped(t, store)
	second, err := NewService(store, Options{20 * time.Millisecond, time.Second, 500 * time.Millisecond}, nil)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err = second.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	go func() { defer close(done); second.Run(ctx) }()
	defer func() { cancel(); <-done }()
	execution, err := first.Pin(ctx, identity)
	if err != nil {
		t.Fatal(err)
	}
	committed, err := store.Apply(execution, verifiedProvider{}, Command{Operation: "role.create", Key: "role:polling", Name: "Polling", ExpectedRevision: first.AppliedRevision()})
	if err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(3 * time.Second)
	for second.AppliedRevision() != committed && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if second.AppliedRevision() != committed {
		t.Fatal("polling did not repair a missed event")
	}
	second.ObserveRevision(committed)
	second.ObserveRevision(committed - 1)
	if err = second.Ready(); err != nil {
		t.Fatal("duplicate/old event invalidated current snapshot", err)
	}
	attempts := 0
	if err = store.Dispatch(ctx, publisherFunc(func(context.Context, Event) error {
		attempts++
		return errors.New("bus unavailable")
	})); err != nil {
		t.Fatal(err)
	}
	if attempts != 2 {
		t.Fatalf("expected bootstrap and create events, got %d", attempts)
	}
	if pending, err := store.OutboxBacklog(ctx); err != nil || pending != 2 {
		t.Fatal("failed publication lost events", pending, err)
	}
	if err = store.db.Exec("UPDATE authz_outbox SET next_attempt_at = ?", time.Now().Add(-time.Second)).Error; err != nil {
		t.Fatal(err)
	}
	if err = store.Dispatch(ctx, publisherFunc(func(_ context.Context, event Event) error {
		if event.Attempts != 1 || event.EventType != "authorization.changed" {
			t.Fatalf("unexpected retry: %+v", event)
		}
		second.ObserveRevision(event.Revision)
		return nil
	})); err != nil {
		t.Fatal(err)
	}
	if pending, err := store.OutboxBacklog(ctx); err != nil || pending != 0 {
		t.Fatal("successful publication remained pending", pending, err)
	}
}

func TestMySQLIncompleteSchemaIsNotReady(t *testing.T) {
	store, _ := mysqlStore(t)
	if err := store.db.Exec("ALTER TABLE authz_outbox DROP COLUMN event_type").Error; err != nil {
		t.Fatal(err)
	}
	service, err := NewService(store, DefaultOptions(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = service.Refresh(context.Background()); !errors.Is(err, ErrUnavailable) {
		t.Fatal("incompatible schema loaded", err)
	}
	if err = service.Ready(); !errors.Is(err, ErrUnavailable) {
		t.Fatal("incompatible schema ready", err)
	}
}
