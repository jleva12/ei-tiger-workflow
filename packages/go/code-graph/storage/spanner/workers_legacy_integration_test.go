//go:build integration

package spannerstore

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
	database "cloud.google.com/go/spanner/admin/database/apiv1"
	"cloud.google.com/go/spanner/admin/database/apiv1/databasepb"
)

func TestWorkerRegistryLegacyRead(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	db, err := database.NewDatabaseAdminClient(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	name := fmt.Sprintf("legacy-%d", time.Now().UnixNano())
	parent := store.database[:strings.LastIndex(store.database, "/databases/")]
	schema, err := schemaFS.ReadFile("schema/002_workers.sql")
	if err != nil {
		t.Fatal(err)
	}
	legacy := strings.TrimSuffix(strings.TrimSpace(strings.ReplaceAll(string(schema), "Languages ARRAY<STRING(64)>", "Language STRING(64)")), ";")
	op, err := db.CreateDatabase(ctx, &databasepb.CreateDatabaseRequest{Parent: parent, CreateStatement: "CREATE DATABASE `" + name + "`", ExtraStatements: []string{legacy}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := op.Wait(ctx); err != nil {
		t.Fatal(err)
	}
	client, err := spanner.NewClient(ctx, parent+"/databases/"+name)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	legacyStore := &Store{client: client}
	started := time.Now().UTC()
	columns := append([]string(nil), workerColumns...)
	columns[3] = "Language"
	if _, err := client.Apply(ctx, []*spanner.Mutation{spanner.Insert("CGWorkers", columns, []any{"ingestion", "old-worker", testConfig, "java", "maven-resolved", int64(6), false, started, spanner.CommitTimestamp})}); err != nil {
		t.Fatal(err)
	}
	workers, err := legacyStore.ActiveWorkers(ctx, "ingestion", time.Minute)
	if err != nil || len(workers) != 1 || len(workers[0].Languages) != 1 || workers[0].Languages[0] != "java" || workers[0].ConfigDigest != testConfig {
		t.Fatalf("legacy worker must remain available for admission: %+v %v", workers, err)
	}
}
