package spannerstore

import (
	"context"
	"fmt"
	"os"
	"regexp"
	"strings"

	"cloud.google.com/go/spanner"
	database "cloud.google.com/go/spanner/admin/database/apiv1"
	"cloud.google.com/go/spanner/admin/database/apiv1/databasepb"
	instance "cloud.google.com/go/spanner/admin/instance/apiv1"
	"cloud.google.com/go/spanner/admin/instance/apiv1/instancepb"
	"google.golang.org/grpc/codes"
)

var schemaObject = regexp.MustCompile(`(?i)^CREATE\s+(?:UNIQUE\s+)?(?:NULL_FILTERED\s+)?(?:SEARCH\s+|VECTOR\s+)?(?:TABLE|INDEX)\s+([A-Za-z0-9_]+)\b`)

// pendingSchema returns the Schema() statements whose table or index is not
// among the existing DDL. Columns of existing tables are never altered, so a
// database keeps the vector length it was created with.
func pendingSchema(existing []string, vectorLength int) ([]string, error) {
	objects := map[string]bool{}
	for _, stmt := range existing {
		if m := schemaObject.FindStringSubmatch(strings.TrimSpace(stmt)); m != nil {
			objects[strings.ToLower(m[1])] = true
		}
	}
	var pending []string
	for _, stmt := range Schema(vectorLength) {
		m := schemaObject.FindStringSubmatch(stmt)
		if m == nil {
			return nil, fmt.Errorf("unrecognized schema statement: %.40s", stmt)
		}
		if !objects[strings.ToLower(m[1])] {
			pending = append(pending, stmt)
		}
	}
	return pending, nil
}

// PendingSchema is pendingSchema for a database outside the emulator: the
// statements an existing database's DDL lacks, to apply with
// UpdateDatabaseDdl (or gcloud spanner databases ddl update).
func PendingSchema(existing []string, vectorLength int) ([]string, error) {
	if !ValidVectorLength(vectorLength) {
		return nil, fmt.Errorf("vector length %d outside 1..%d", vectorLength, MaxVectorLength)
	}
	return pendingSchema(existing, vectorLength)
}

// ProvisionEmulator creates the instance and database named by the full
// database path on the emulator at SPANNER_EMULATOR_HOST, applying the schema
// for vectorLength-dimensional embeddings and, for an existing database, any
// missing tables or indexes. Idempotent.
func ProvisionEmulator(ctx context.Context, databaseName string, vectorLength int) error {
	if !ValidVectorLength(vectorLength) {
		return fmt.Errorf("vector length %d outside 1..%d", vectorLength, MaxVectorLength)
	}
	if os.Getenv("SPANNER_EMULATOR_HOST") == "" {
		return fmt.Errorf("SPANNER_EMULATOR_HOST is not set; refusing to provision a non-emulator database")
	}
	parts := strings.Split(databaseName, "/")
	if len(parts) != 6 || parts[0] != "projects" || parts[2] != "instances" || parts[4] != "databases" || !regexp.MustCompile(`^[a-z][a-z0-9_-]{0,127}$`).MatchString(parts[5]) {
		return fmt.Errorf("invalid emulator database name %q", databaseName)
	}
	project := strings.Join(parts[:2], "/")
	inst := strings.Join(parts[:4], "/")
	instances, err := instance.NewInstanceAdminClient(ctx)
	if err != nil {
		return err
	}
	defer instances.Close()
	if _, err = instances.GetInstance(ctx, &instancepb.GetInstanceRequest{Name: inst}); spanner.ErrCode(err) == codes.NotFound {
		op, e := instances.CreateInstance(ctx, &instancepb.CreateInstanceRequest{Parent: project, InstanceId: parts[3], Instance: &instancepb.Instance{Name: inst, Config: project + "/instanceConfigs/emulator-config", DisplayName: parts[3], NodeCount: 1}})
		if e != nil && spanner.ErrCode(e) != codes.AlreadyExists {
			return e
		}
		if e == nil {
			if _, e = op.Wait(ctx); e != nil {
				return e
			}
		}
	} else if err != nil {
		return err
	}
	dbs, err := database.NewDatabaseAdminClient(ctx)
	if err != nil {
		return err
	}
	defer dbs.Close()
	_, err = dbs.GetDatabase(ctx, &databasepb.GetDatabaseRequest{Name: databaseName})
	if spanner.ErrCode(err) == codes.NotFound {
		op, e := dbs.CreateDatabase(ctx, &databasepb.CreateDatabaseRequest{Parent: inst, CreateStatement: "CREATE DATABASE `" + parts[5] + "`", ExtraStatements: Schema(vectorLength)})
		if e != nil && spanner.ErrCode(e) != codes.AlreadyExists {
			return e
		}
		if e == nil {
			_, e = op.Wait(ctx)
			return e
		}
	} else if err != nil {
		return err
	}
	ddl, err := dbs.GetDatabaseDdl(ctx, &databasepb.GetDatabaseDdlRequest{Database: databaseName})
	if err != nil {
		return err
	}
	pending, err := pendingSchema(ddl.Statements, vectorLength)
	if err != nil || len(pending) == 0 {
		return err
	}
	op, err := dbs.UpdateDatabaseDdl(ctx, &databasepb.UpdateDatabaseDdlRequest{Database: databaseName, Statements: pending})
	if err != nil {
		return fmt.Errorf("apply schema: %w", err)
	}
	return op.Wait(ctx)
}
