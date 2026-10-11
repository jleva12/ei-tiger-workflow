//go:build integration

package ingestion_test

import (
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"testing"

	"cloud.google.com/go/spanner"
	"google.golang.org/api/iterator"

	"ei-aitiger-codegraph/pkg/graph"
)

// graphRows reads what ingestion wrote straight from the Spanner tables, the
// way the readers (the Python package forge_codegraph_query) do; the store
// itself keeps only the reads ingestion needs.
type graphRows struct {
	t      *testing.T
	client *spanner.Client
	repo   string
}

func openGraphRows(ctx context.Context, t *testing.T, database, repo string) *graphRows {
	t.Helper()
	client, err := spanner.NewClient(ctx, database)
	if err != nil {
		t.Fatalf("spanner client: %v", err)
	}
	t.Cleanup(client.Close)
	return &graphRows{t: t, client: client, repo: repo}
}

const recordColumns = "GenFrom, GenTo, CommitFrom, CommitTo, Retired, Lineage, FactDigest, Payload"

func (r *graphRows) query(ctx context.Context, sql string, params map[string]any, each func(*spanner.Row)) {
	r.t.Helper()
	params["repo"] = r.repo
	it := r.client.Single().Query(ctx, spanner.Statement{SQL: sql, Params: params})
	defer it.Stop()
	for {
		row, err := it.Next()
		if err == iterator.Done {
			return
		}
		if err != nil {
			r.t.Fatalf("%s: %v", sql, err)
		}
		each(row)
	}
}

func (r *graphRows) version(row *spanner.Row) graph.Version {
	r.t.Helper()
	var genFrom int64
	var genTo spanner.NullInt64
	var lineage spanner.NullString
	var payload []byte
	v := graph.Version{}
	if err := row.Columns(&genFrom, &genTo, &v.CommitFrom, &v.CommitTo, &v.Retired, &lineage, &v.FactDigest, &payload); err != nil {
		r.t.Fatal(err)
	}
	v.GenFrom, v.GenTo, v.Lineage = uint64(genFrom), uint64(genTo.Int64), lineage.StringVal
	if err := json.Unmarshal(payload, &v.Fact); err != nil {
		r.t.Fatalf("record payload: %v", err)
	}
	return v
}

// live is the repository's live generation.
func (r *graphRows) live(ctx context.Context) uint64 {
	r.t.Helper()
	var live int64
	r.query(ctx, "SELECT LiveGeneration FROM CGRepositories WHERE RepositoryID=@repo", map[string]any{}, func(row *spanner.Row) {
		if err := row.Columns(&live); err != nil {
			r.t.Fatal(err)
		}
	})
	return uint64(live)
}

// open returns the records of one kind (node or edge) and fact kind open at
// generation, zero meaning live.
func (r *graphRows) open(ctx context.Context, kind graph.RecordKind, factKind string, generation uint64) []graph.Version {
	r.t.Helper()
	if generation == 0 {
		generation = r.live(ctx)
	}
	var out []graph.Version
	r.query(ctx, "SELECT "+recordColumns+" FROM CGRecords WHERE RepositoryID=@repo AND RecordKind=@kind AND (@factKind='' OR Kind=@factKind) AND GenFrom<=@gen AND (GenTo IS NULL OR GenTo>@gen) ORDER BY RecordID",
		map[string]any{"kind": string(kind), "factKind": factKind, "gen": int64(generation)}, func(row *spanner.Row) { out = append(out, r.version(row)) })
	return out
}

// history returns every version of a record, oldest first.
func (r *graphRows) history(ctx context.Context, key graph.Key) []graph.Version {
	r.t.Helper()
	var out []graph.Version
	r.query(ctx, "SELECT "+recordColumns+" FROM CGRecords WHERE RepositoryID=@repo AND RecordKind=@kind AND RecordID=@id ORDER BY GenFrom",
		map[string]any{"kind": string(key.Kind), "id": key.ID}, func(row *spanner.Row) { out = append(out, r.version(row)) })
	return out
}

// searchHashes maps each node open at live that carries a search document to
// its record's search hash.
func (r *graphRows) searchHashes(ctx context.Context) map[string]string {
	r.t.Helper()
	out := map[string]string{}
	r.query(ctx, "SELECT RecordID, SearchHash FROM CGRecords WHERE RepositoryID=@repo AND RecordKind='node' AND SearchHash IS NOT NULL AND GenFrom<=@gen AND (GenTo IS NULL OR GenTo>@gen)",
		map[string]any{"gen": int64(r.live(ctx))}, func(row *spanner.Row) {
			var id, hash string
			if err := row.Columns(&id, &hash); err != nil {
				r.t.Fatal(err)
			}
			out[id] = hash
		})
	return out
}

// searchDocument is a node's CGSearchDocuments row.
type searchDocument struct {
	Kind, Name, NameLower, QualifiedName, FilePath, Hash, Text, Identifiers string
	Version                                                                 int64
}

func (r *graphRows) searchDocuments(ctx context.Context) map[string]searchDocument {
	r.t.Helper()
	out := map[string]searchDocument{}
	r.query(ctx, "SELECT NodeID, Kind, Name, NameLower, QualifiedName, FilePath, DocumentHash, DocumentVersion, DocumentText, Identifiers FROM CGSearchDocuments WHERE RepositoryID=@repo",
		map[string]any{}, func(row *spanner.Row) {
			var id string
			var d searchDocument
			if err := row.Columns(&id, &d.Kind, &d.Name, &d.NameLower, &d.QualifiedName, &d.FilePath, &d.Hash, &d.Version, &d.Text, &d.Identifiers); err != nil {
				r.t.Fatal(err)
			}
			out[id] = d
		})
	return out
}

// named returns the IDs of the search documents with this lower-case name,
// through the index exact-name lookups use.
func (r *graphRows) named(ctx context.Context, lower string) []string {
	r.t.Helper()
	var out []string
	r.query(ctx, "SELECT NodeID FROM CGSearchDocuments@{FORCE_INDEX=CGSearchDocumentsByName} WHERE RepositoryID=@repo AND NameLower=@name ORDER BY NodeID",
		map[string]any{"name": lower}, func(row *spanner.Row) {
			var id string
			if err := row.Columns(&id); err != nil {
				r.t.Fatal(err)
			}
			out = append(out, id)
		})
	return out
}

// embedding is a node's CGSearchEmbeddings row for one model.
type embedding struct {
	Hash    string
	Version int64
	Vector  []float32
}

func (r *graphRows) embeddings(ctx context.Context, model string, dimensions int) map[string]embedding {
	r.t.Helper()
	out := map[string]embedding{}
	r.query(ctx, "SELECT NodeID, DocumentHash, DocumentVersion, Embedding FROM CGSearchEmbeddings WHERE RepositoryID=@repo AND Model=@model AND Dimensions=@dims",
		map[string]any{"model": model, "dims": int64(dimensions)}, func(row *spanner.Row) {
			var id string
			var e embedding
			if err := row.Columns(&id, &e.Hash, &e.Version, &e.Vector); err != nil {
				r.t.Fatal(err)
			}
			out[id] = e
		})
	return out
}

// content returns retained source bytes, checked against the hash that keys
// them.
func (r *graphRows) content(ctx context.Context, sha string) []byte {
	r.t.Helper()
	var data []byte
	found := false
	r.query(ctx, "SELECT UncompressedBytes, Payload FROM CGContent WHERE RepositoryID=@repo AND SHA256=@sha", map[string]any{"sha": sha}, func(row *spanner.Row) {
		var size int64
		var z []byte
		if err := row.Columns(&size, &z); err != nil {
			r.t.Fatal(err)
		}
		reader, err := gzip.NewReader(bytes.NewReader(z))
		if err != nil {
			r.t.Fatalf("content %s: %v", sha, err)
		}
		if data, err = io.ReadAll(reader); err != nil || int64(len(data)) != size {
			r.t.Fatalf("content %s: %d bytes, want %d: %v", sha, len(data), size, err)
		}
		found = true
	})
	if sum := sha256.Sum256(data); !found || hex.EncodeToString(sum[:]) != sha {
		r.t.Fatalf("content %s missing or not matching its hash", sha)
	}
	return data
}
