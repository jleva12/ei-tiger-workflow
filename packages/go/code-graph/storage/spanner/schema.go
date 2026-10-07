package spannerstore

import (
	"embed"
	"fmt"
	"sort"
	"strconv"
	"strings"
)

//go:embed schema/*.sql
var schemaFS embed.FS

const (
	// vectorLengthPlaceholder is replaced by the configured embedding
	// dimension: a Spanner vector index needs a fixed-length column.
	vectorLengthPlaceholder = "{{VECTOR_LENGTH}}"
	// MaxVectorLength bounds the embedding dimension a database is created with.
	MaxVectorLength = 4096
)

// ValidVectorLength reports whether n is an embedding dimension a database
// can be provisioned or opened with.
func ValidVectorLength(n int) bool { return n >= 1 && n <= MaxVectorLength }

// Schema returns the DDL statements that define the durable backend, in
// application order, for embeddings of vectorLength dimensions. Files under
// schema/ are applied in lexical order and each file is split on ";". The
// embedding column is ARRAY<FLOAT32>(vector_length=>N) with a VECTOR INDEX
// over it; the dimension is part of the database and cannot change without a
// new column and index.
func Schema(vectorLength int) []string {
	if !ValidVectorLength(vectorLength) {
		panic(fmt.Sprintf("spanner schema: vector length %d outside 1..%d", vectorLength, MaxVectorLength))
	}
	entries, err := schemaFS.ReadDir("schema")
	if err != nil {
		panic(err)
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".sql") {
			names = append(names, e.Name())
		}
	}
	sort.Strings(names)
	var out []string
	for _, name := range names {
		b, err := schemaFS.ReadFile("schema/" + name)
		if err != nil {
			panic(err)
		}
		text := strings.ReplaceAll(string(b), vectorLengthPlaceholder, strconv.Itoa(vectorLength))
		for _, stmt := range strings.Split(text, ";") {
			if stmt = strings.TrimSpace(stmt); stmt != "" {
				out = append(out, stmt)
			}
		}
	}
	return out
}
