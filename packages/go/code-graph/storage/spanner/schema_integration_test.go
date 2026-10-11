//go:build integration

package spannerstore

import (
	"context"
	"fmt"
	"testing"
	"time"

	"cloud.google.com/go/spanner"
	database "cloud.google.com/go/spanner/admin/database/apiv1"
	"cloud.google.com/go/spanner/admin/database/apiv1/databasepb"
)

// A database made before cross-repository links has no CGCrossLinks table;
// provisioning it again adds just what is missing, so the readers, which
// keep the links there, find the table and its indexes.
func TestProvisioningAddsMissingTables(t *testing.T) {
	ctx := context.Background()
	db := fmt.Sprintf("projects/codegraph-test/instances/test/databases/cg-old-%d", time.Now().UnixNano())
	if err := ProvisionEmulator(ctx, db, testVectorLength); err != nil {
		t.Fatalf("provision: %v", err)
	}
	admin, err := database.NewDatabaseAdminClient(ctx)
	if err != nil {
		t.Fatalf("admin client: %v", err)
	}
	defer admin.Close()
	op, err := admin.UpdateDatabaseDdl(ctx, &databasepb.UpdateDatabaseDdlRequest{Database: db, Statements: []string{"DROP INDEX CGCrossLinksBySource", "DROP INDEX CGCrossLinksByTarget", "DROP TABLE CGCrossLinks"}})
	if err == nil {
		err = op.Wait(ctx)
	}
	if err != nil {
		t.Fatalf("drop: %v", err)
	}
	ddl, err := admin.GetDatabaseDdl(ctx, &databasepb.GetDatabaseDdlRequest{Database: db})
	if err != nil {
		t.Fatalf("ddl: %v", err)
	}
	pending, err := PendingSchema(ddl.Statements, testVectorLength)
	if err != nil || len(pending) != 3 {
		t.Fatalf("pending after the drop: %q %v", pending, err)
	}
	if err := ProvisionEmulator(ctx, db, testVectorLength); err != nil {
		t.Fatalf("re-provision: %v", err)
	}
	if ddl, err = admin.GetDatabaseDdl(ctx, &databasepb.GetDatabaseDdlRequest{Database: db}); err != nil {
		t.Fatalf("ddl: %v", err)
	}
	if pending, err = PendingSchema(ddl.Statements, testVectorLength); err != nil || len(pending) != 0 {
		t.Fatalf("pending after re-provisioning: %q %v", pending, err)
	}
	client, err := spanner.NewClient(ctx, db)
	if err != nil {
		t.Fatalf("client: %v", err)
	}
	defer client.Close()
	for _, side := range []string{"Source", "Target"} {
		it := client.Single().Query(ctx, spanner.Statement{SQL: "SELECT COUNT(*) FROM CGCrossLinks@{FORCE_INDEX=CGCrossLinksBy" + side + "} WHERE " + side + "RepositoryID=@repo", Params: map[string]any{"repo": "repo:any"}})
		row, err := nextRow(it)
		it.Stop()
		var n int64
		if err == nil && row != nil {
			err = row.Columns(&n)
		}
		if err != nil || row == nil || n != 0 {
			t.Fatalf("CGCrossLinks by %s after re-provisioning: %d %v", side, n, err)
		}
	}
}
