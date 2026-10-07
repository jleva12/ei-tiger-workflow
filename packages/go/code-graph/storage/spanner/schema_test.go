package spannerstore

import (
	"strings"
	"testing"
)

func TestSchemaStatements(t *testing.T) {
	stmts := Schema(1536)
	want := []string{"CGRepositories", "CGRuns", "CGRunsByIdentity", "CGRunsByPhase", "CGSubmissions", "CGRecords", "CGRecordsByLineage", "CGRecordsByKind", "CGEdgesBySource", "CGEdgesByTarget", "CGRecordsByGenTo", "CGRecordsByGenFrom", "CGFileIdentities", "CGFileIdentitiesByGeneration", "CGFileIdentitiesByPath", "CGContent", "CGSearchEmbeddings", "CGSearchEmbeddingsVector", "CGGenerationInputs", "CGSearchDocuments", "CGSearchDocumentsByName", "CGSearchDocumentsIndex", "CGWorkers"}
	want = append(want, "CGOrganizationEntities", "CGOrganizationSlugs", "CGOrganizationGraphOwners")
	want = append(want, "CGCrossLinks", "CGCrossLinksBySource", "CGCrossLinksByTarget")
	if len(stmts) != len(want) {
		t.Fatalf("got %d statements, want %d", len(stmts), len(want))
	}
	for i, stmt := range stmts {
		m := schemaObject.FindStringSubmatch(stmt)
		if m == nil || m[1] != want[i] {
			t.Errorf("statement %d creates %v, want %s", i, m, want[i])
		}
		if strings.HasSuffix(stmt, ";") {
			t.Errorf("statement %d keeps its terminator", i)
		}
		if strings.Contains(stmt, vectorLengthPlaceholder) {
			t.Errorf("statement %d keeps the vector length placeholder", i)
		}
	}
	if !strings.Contains(stmts[16], "ARRAY<FLOAT32>(vector_length=>1536) NOT NULL") || !strings.Contains(stmts[17], "VECTOR INDEX CGSearchEmbeddingsVector ON CGSearchEmbeddings(Embedding)") {
		t.Fatalf("embedding column and index: %s\n%s", stmts[16], stmts[17])
	}
	defer func() {
		if recover() == nil {
			t.Fatal("an invalid vector length must panic")
		}
	}()
	Schema(0)
}

func TestPendingSchema(t *testing.T) {
	all := Schema(4)
	pending, err := pendingSchema(nil, 4)
	if err != nil || len(pending) != len(all) {
		t.Fatalf("empty database needs everything: %d %v", len(pending), err)
	}
	pending, err = pendingSchema(all, 4)
	if err != nil || len(pending) != 0 {
		t.Fatalf("complete database needs nothing: %d %v", len(pending), err)
	}
	existing := []string{"CREATE TABLE cgrecords (\n RepositoryID STRING(256) NOT NULL\n) PRIMARY KEY (RepositoryID)", "CREATE INDEX CGRecordsByKind ON CGRecords(RepositoryID)"}
	pending, err = pendingSchema(existing, 4)
	if err != nil || len(pending) != len(all)-2 {
		t.Fatalf("names match case-insensitively: %d %v", len(pending), err)
	}
	for _, stmt := range pending {
		if m := schemaObject.FindStringSubmatch(stmt); m[1] == "CGRecords" || m[1] == "CGRecordsByKind" {
			t.Errorf("existing object re-created: %s", m[1])
		}
	}
}

func TestOrganizationMigrationIsAdditive(t *testing.T) {
	pending := PendingOrganizationSchema(nil)
	if len(pending) != 3 {
		t.Fatalf("expected table and two indexes, got %d", len(pending))
	}
	if got := PendingOrganizationSchema(append([]string{"CREATE TABLE Unrelated (ID INT64) PRIMARY KEY (ID)"}, pending...)); len(got) != 0 {
		t.Fatalf("migration not idempotent: %v", got)
	}
	if got := PendingOrganizationSchema(pending[:1]); len(got) != 2 {
		t.Fatalf("partial migration: %v", got)
	}
}
