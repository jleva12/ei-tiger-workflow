package authorization

import (
	"context"
	"errors"
	"testing"
)

func TestMySQLUserIntegrationLifecycle(t *testing.T) {
	store, _ := mysqlStore(t)
	ctx := context.Background()
	link := UserIntegration{TenantID: "tenant", SubjectID: "user:alice", Provider: "github", AccountID: "12", AccountLogin: "octocat", Credential: []byte{1, 2, 3}}
	if _, err := store.UserIntegration(ctx, "tenant", "user:alice", "github"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("missing link: %v", err)
	}
	if _, err := store.ConnectUserIntegration(ctx, UserIntegration{TenantID: "tenant", SubjectID: "alice", Provider: "github", AccountID: "12", AccountLogin: "octocat", Credential: []byte{1}}); !errors.Is(err, ErrInvalid) {
		t.Fatal("accepted a subject without the user: prefix")
	}
	first, err := store.ConnectUserIntegration(ctx, link)
	if err != nil || first.Revision != 1 || first.Status != IntegrationActive {
		t.Fatalf("connect: %+v %v", first, err)
	}
	// A rotation from the current revision wins; a stale one conflicts.
	rotated := first
	rotated.Credential = []byte{4, 5, 6}
	if rotated, err = store.UpdateUserIntegration(ctx, rotated); err != nil || rotated.Revision != 2 {
		t.Fatalf("rotate: %+v %v", rotated, err)
	}
	stale := first
	stale.Credential = []byte{9}
	if _, err = store.UpdateUserIntegration(ctx, stale); !errors.Is(err, ErrConflict) {
		t.Fatalf("stale rotation: %v", err)
	}
	read, err := store.UserIntegration(ctx, "tenant", "user:alice", "github")
	if err != nil || string(read.Credential) != string([]byte{4, 5, 6}) || read.Revision != 2 || !read.ConnectedAt.Equal(first.ConnectedAt) {
		t.Fatalf("read after rotation: %+v %v", read, err)
	}
	// Another person's link is separate.
	if _, err = store.UserIntegration(ctx, "tenant", "user:bob", "github"); !errors.Is(err, ErrNotFound) {
		t.Fatal("link visible to another person")
	}
	// Reconnecting replaces the account and keeps counting revisions.
	again := link
	again.AccountID, again.AccountLogin, again.Status = "13", "hubot", IntegrationExpired
	if again, err = store.ConnectUserIntegration(ctx, again); err != nil || again.Revision != 3 || again.Status != IntegrationActive {
		t.Fatalf("reconnect: %+v %v", again, err)
	}
	if err = store.DeleteUserIntegration(ctx, "tenant", "user:alice", "github"); err != nil {
		t.Fatal(err)
	}
	if err = store.DeleteUserIntegration(ctx, "tenant", "user:alice", "github"); err != nil {
		t.Fatal("deleting a missing link failed")
	}
	if _, err = store.UserIntegration(ctx, "tenant", "user:alice", "github"); !errors.Is(err, ErrNotFound) {
		t.Fatal("link survived disconnect")
	}
}
