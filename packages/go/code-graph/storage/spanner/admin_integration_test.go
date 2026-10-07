//go:build integration

package spannerstore

import (
	"context"
	"testing"
)

func TestRepositoryListingPaginates(t *testing.T) {
	ctx := context.Background()
	repo := "admin-controls"
	register(t, repo)
	register(t, repo+"-2")
	// Exercise repository pagination with real signed cursors.
	found := false
	cursor := ""
	for {
		repositories, err := store.ListRepositories(ctx, 1, cursor)
		if err != nil {
			t.Fatal(err)
		}
		if len(repositories.Items) > 1 {
			t.Fatalf("page over its limit: %+v", repositories)
		}
		for _, r := range repositories.Items {
			found = found || r.Repository.RepositoryID == repo
		}
		cursor = repositories.NextCursor
		if cursor == "" {
			break
		}
	}
	if !found {
		t.Fatal("repository absent")
	}
}
