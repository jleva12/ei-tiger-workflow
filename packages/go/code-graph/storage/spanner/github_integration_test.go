//go:build integration

package spannerstore

import (
	"context"
	"testing"
)

func TestGitHubAppRegistrationRoundTrip(t *testing.T) {
	ctx := context.Background()
	repo := register(t, "github-app-link")
	_, err := store.AdmitIngestion(ctx, minimalAdmission(repo.RepositoryID, "github-link-first"))
	if err != nil {
		t.Fatal(err)
	}
	repo, err = store.GetRepository(ctx, repo.RepositoryID)
	if err != nil {
		t.Fatal(err)
	}
	branch, commit := repo.Branch, repo.LastIngestionCommit
	repo.GitHubRepositoryID = 22
	repo.GitHubInstallationID = 8
	repo.DefaultBranch = "main"
	repo.IntegrationID = "github-app"
	linked, err := store.PutRepository(ctx, repo)
	if err != nil {
		t.Fatal(err)
	}
	got, err := store.GetRepository(ctx, repo.RepositoryID)
	if err != nil || got != linked || got.Branch != branch || got.LastIngestionCommit != commit {
		t.Fatalf("app metadata round trip %+v %v", got, err)
	}
}

func TestPublicRegistrationRoundTripAndAdmission(t *testing.T) {
	ctx := context.Background()
	repo := register(t, "github-public-url")
	repo.Public, repo.DefaultBranch, repo.IntegrationID = true, "main", "github-public"
	repo, err := store.PutRepository(ctx, repo)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.AdmitIngestion(ctx, minimalAdmission(repo.RepositoryID, "public-first")); err != nil {
		t.Fatal(err)
	}
	got, err := store.GetRepository(ctx, repo.RepositoryID)
	if err != nil || !got.Public || got.Branch != "main" || got.LastIngestionCommit == "" || got.GitHubInstallationID != 0 {
		t.Fatalf("public flag lost during admission: %+v %v", got, err)
	}
	branch, commit := got.Branch, got.LastIngestionCommit
	got.Public, got.IntegrationID, got.GitHubRepositoryID, got.GitHubInstallationID = false, "github-app", 22, 8
	if _, err := store.PutRepository(ctx, got); err != nil {
		t.Fatal(err)
	}
	got, err = store.GetRepository(ctx, repo.RepositoryID)
	if err != nil || got.Public || got.GitHubInstallationID != 8 || got.Branch != branch || got.LastIngestionCommit != commit {
		t.Fatalf("access switch lost history: %+v %v", got, err)
	}
}
