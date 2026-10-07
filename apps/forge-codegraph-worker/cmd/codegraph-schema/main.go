// Command codegraph-schema prints the database DDL for one embedding
// dimension, in application order: the statements the worker provisions the
// emulator with. make worker-spanner-cloud feeds it to
// `gcloud spanner databases create --ddl-file`, so a managed database is
// created from the same source as the emulator. With -existing, it prints
// only the tables and indexes an existing database's DDL (the JSON of
// `gcloud spanner databases ddl describe --format=json`) lacks, for make
// worker-spanner-cloud-update to add; it never alters or drops anything.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strconv"
	"strings"

	"ei-aitiger-codegraph/pkg/codesearch"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
)

func main() {
	vectorLength := flag.Int("vector-length", defaultVectorLength(), "embedding dimension of the vector column (CODEGRAPH_EMBEDDING_DIMENSIONS when set and nonzero)")
	existing := flag.String("existing", "", "print only what this database DDL (a JSON list of statements) lacks")
	flag.Parse()
	if flag.NArg() != 0 || !spannerstore.ValidVectorLength(*vectorLength) {
		fmt.Fprintf(os.Stderr, "usage: codegraph-schema [-vector-length 1..%d] [-existing ddl.json]\n", spannerstore.MaxVectorLength)
		os.Exit(2)
	}
	if *existing != "" {
		pending, err := pendingFor(*existing, *vectorLength)
		if err != nil {
			fmt.Fprintln(os.Stderr, "codegraph-schema:", err)
			os.Exit(1)
		}
		if len(pending) > 0 {
			fmt.Println(strings.Join(pending, ";\n\n") + ";")
		}
		return
	}
	statements := spannerstore.Schema(*vectorLength)
	fmt.Printf("-- codegraph schema, %d statements, vector length %d\n", len(statements), *vectorLength)
	fmt.Println(strings.Join(statements, ";\n\n") + ";")
}

// pendingFor reads a database's DDL as gcloud describes it in JSON, a list
// of statements, and returns the schema statements it lacks.
func pendingFor(path string, vectorLength int) ([]string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var ddl []string
	if err := json.Unmarshal(b, &ddl); err != nil {
		return nil, fmt.Errorf("%s is not a JSON list of DDL statements: %w", path, err)
	}
	return spannerstore.PendingSchema(ddl, vectorLength)
}

func defaultVectorLength() int {
	if n, err := strconv.Atoi(strings.TrimSpace(os.Getenv("CODEGRAPH_EMBEDDING_DIMENSIONS"))); err == nil && n > 0 {
		return n
	}
	return codesearch.DefaultDimensions
}
