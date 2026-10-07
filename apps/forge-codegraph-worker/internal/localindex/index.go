// Package localindex is the disposable, run-local SQLite index behind
// semantic.Workspace, plus the persistent syntax cache shared across runs.
//
// One index.sqlite per run, created fresh by Open and deleted by Close. The
// file is in WAL mode because the resolver, matcher and projector read while
// the writer commits batches: in rollback-journal mode (journal_mode=OFF
// included) every commit takes the EXCLUSIVE lock, which waits for in-flight
// readers and blocks new ones, so each batch commit would stall concurrent
// point reads and vice versa. WAL readers work on snapshots and never block
// the writer. synchronous=OFF removes every fsync, so WAL adds no durability
// work; wal_autocheckpoint is raised so index pages rewritten by many small
// batches reach the main file once per checkpoint rather than once per batch.
//
// There is exactly one write connection, serialized by a mutex, one prepared
// statement per query, and one transaction per batch. A pool of read
// connections shares the same prepared statements (database/sql prepares each
// once per connection). Rows are opaque JSON payloads keyed by ID: reads
// decode and return them without re-hashing or re-validating anything.
package localindex

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"

	"golang.org/x/sync/singleflight"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

// Options tunes the run index. Zero values take the documented defaults.
type Options struct {
	// CacheBytes is the SQLite page cache per connection (default 256 MiB).
	// The main file is also mmap'd, so reader caches mostly hold WAL pages.
	CacheBytes int64
	// PreviousCacheEntries bounds the in-memory LRU of decoded previous
	// identity maps (default 4096); everything fetched stays in the
	// previous table, so an eviction costs a decode, not a remote read.
	PreviousCacheEntries int
}

func (o Options) withDefaults() Options {
	if o.CacheBytes <= 0 {
		o.CacheBytes = 256 << 20
	}
	if o.PreviousCacheEntries <= 0 {
		o.PreviousCacheEntries = 4096
	}
	return o
}

const (
	indexFile     = "index.sqlite"
	syntaxLRUSize = 8
	symbolPage    = 1024
	maxIn         = 500
)

// inBuckets are the prepared IN-list sizes for Symbols. A chunk is padded to
// the next bucket by repeating its last ID, which is harmless in an IN list.
var inBuckets = [...]int{1, 2, 4, 8, 16, 32, 64, 128, 256, maxIn}

var errClosed = errors.New("localindex: index is closed")

const indexSchema = `
CREATE TABLE files (
	file_id       TEXT    PRIMARY KEY,
	lineage       TEXT    NOT NULL,
	module_id     TEXT    NOT NULL,
	source_set_id TEXT    NOT NULL,
	path          TEXT    NOT NULL,
	affected      INTEGER NOT NULL,
	source        BLOB    NOT NULL
);
CREATE UNIQUE INDEX files_by_path ON files(source_set_id, path);
CREATE INDEX files_order ON files(module_id, source_set_id, path);
CREATE TABLE symbols (
	id      TEXT PRIMARY KEY,
	file_id TEXT NOT NULL,
	payload BLOB NOT NULL
);
CREATE INDEX symbols_by_file ON symbols(file_id, id);
CREATE TABLE lookups (
	file_id TEXT    NOT NULL,
	ordinal INTEGER NOT NULL,
	payload BLOB    NOT NULL,
	PRIMARY KEY (file_id, ordinal)
) WITHOUT ROWID;
CREATE TABLE identities (
	file_id TEXT PRIMARY KEY,
	lineage TEXT NOT NULL,
	payload BLOB NOT NULL
);
CREATE TABLE entities (
	file_id        TEXT NOT NULL,
	declaration_id TEXT NOT NULL,
	entity_id      TEXT NOT NULL,
	PRIMARY KEY (file_id, declaration_id)
) WITHOUT ROWID;
CREATE TABLE previous (
	lineage TEXT    PRIMARY KEY,
	found   INTEGER NOT NULL,
	payload BLOB
);
`

func indexPragmas(o Options, writer bool) []string {
	p := []string{
		"PRAGMA synchronous=OFF",
		"PRAGMA temp_store=MEMORY",
		fmt.Sprintf("PRAGMA cache_size=-%d", o.CacheBytes/1024),
		"PRAGMA mmap_size=1073741824",
		"PRAGMA foreign_keys=OFF",
	}
	if writer {
		p = append([]string{"PRAGMA journal_mode=WAL"}, p...)
		return append(p, "PRAGMA wal_autocheckpoint=10000")
	}
	return append(p, "PRAGMA query_only=ON")
}

// Index is the run-local semantic.Workspace. See the package documentation
// for the storage design. Reads may run concurrently with each other and
// with writes; writes are serialized.
type Index struct {
	path     string
	build    bc.BuildContext
	root     *os.Root
	syntax   *SyntaxCache
	digest   string
	sets     map[string]bc.SourceSet
	previous semantic.PreviousIdentityReader

	writer  *sql.DB
	readers *sql.DB
	wmu     sync.Mutex
	stmts   []*sql.Stmt

	insFile, updAffected, insSymbol, delLookups, insLookup        *sql.Stmt
	insIdentities, delEntities, insEntity, insPrevious            *sql.Stmt
	selFiles, selFile, selFileByPath, selSymbol, selSymbolsByFile *sql.Stmt
	selSymbolsFirst, selSymbolsAfter, selLookups, selIdentities   *sql.Stmt
	selEntity, selPrevious                                        *sql.Stmt
	selSymbolsIn                                                  [len(inBuckets)]*sql.Stmt

	syntaxLRU  *lru[string, ir.SourceFile]
	prevLRU    *lru[string, previousEntry]
	prevFlight singleflight.Group
	aliasMu    sync.RWMutex
	aliases    map[string]string

	closed atomic.Bool
}

var _ semantic.Workspace = (*Index)(nil)

// Open creates <dir>/index.sqlite, deleting any existing one. checkoutPath
// is the directory SourceBytes reads from (through os.Root, so paths cannot
// escape it). syntax may be nil, in which case Syntax reports ErrNotFound;
// previous may be nil for a first generation.
func Open(ctx context.Context, dir string, build bc.BuildContext, checkoutPath string, syntax *SyntaxCache, descriptorDigest string, previous semantic.PreviousIdentityReader, o Options) (_ *Index, err error) {
	o = o.withDefaults()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	p := filepath.Join(dir, indexFile)
	if err := removeDB(p); err != nil {
		return nil, err
	}
	root, err := os.OpenRoot(checkoutPath)
	if err != nil {
		return nil, fmt.Errorf("localindex: checkout: %w", err)
	}
	x := &Index{
		path:      p,
		build:     build,
		root:      root,
		syntax:    syntax,
		digest:    descriptorDigest,
		sets:      make(map[string]bc.SourceSet, len(build.Inventory.SourceSets)),
		previous:  previous,
		syntaxLRU: newLRU[string, ir.SourceFile](syntaxLRUSize),
		prevLRU:   newLRU[string, previousEntry](o.PreviousCacheEntries),
		aliases:   map[string]string{},
	}
	for _, set := range build.Inventory.SourceSets {
		x.sets[string(set.ID)] = set
	}
	defer func() {
		if err != nil {
			_ = x.Close()
		}
	}()
	x.writer = openPool(newConnector(p, "immediate", indexPragmas(o, true)), 1)
	if _, err = x.writer.ExecContext(ctx, indexSchema); err != nil {
		return nil, fmt.Errorf("localindex: schema: %w", err)
	}
	x.readers = openPool(newConnector(p, "", indexPragmas(o, false)), readConns())

	w := &preparer{ctx: ctx, db: x.writer, all: &x.stmts}
	x.insFile = w.prep(`INSERT OR REPLACE INTO files(file_id, lineage, module_id, source_set_id, path, affected, source) VALUES (?, ?, ?, ?, ?, ?, ?)`)
	x.updAffected = w.prep(`UPDATE files SET affected = 1 WHERE file_id = ?`)
	x.insSymbol = w.prep(`INSERT OR REPLACE INTO symbols(id, file_id, payload) VALUES (?, ?, ?)`)
	x.delLookups = w.prep(`DELETE FROM lookups WHERE file_id = ?`)
	x.insLookup = w.prep(`INSERT INTO lookups(file_id, ordinal, payload) VALUES (?, ?, ?)`)
	x.insIdentities = w.prep(`INSERT OR REPLACE INTO identities(file_id, lineage, payload) VALUES (?, ?, ?)`)
	x.delEntities = w.prep(`DELETE FROM entities WHERE file_id = ?`)
	x.insEntity = w.prep(`INSERT OR REPLACE INTO entities(file_id, declaration_id, entity_id) VALUES (?, ?, ?)`)
	x.insPrevious = w.prep(`INSERT OR REPLACE INTO previous(lineage, found, payload) VALUES (?, ?, ?)`)

	r := &preparer{ctx: ctx, db: x.readers, all: &x.stmts}
	x.selFiles = r.prep(`SELECT lineage, affected, source FROM files ORDER BY module_id, source_set_id, path`)
	x.selFile = r.prep(`SELECT lineage, affected, source FROM files WHERE file_id = ?`)
	x.selFileByPath = r.prep(`SELECT lineage, affected, source FROM files WHERE source_set_id = ? AND path = ?`)
	x.selSymbol = r.prep(`SELECT payload FROM symbols WHERE id = ?`)
	x.selSymbolsByFile = r.prep(`SELECT payload FROM symbols WHERE file_id = ? ORDER BY id`)
	x.selSymbolsFirst = r.prep(`SELECT id, payload FROM symbols ORDER BY id LIMIT ?`)
	x.selSymbolsAfter = r.prep(`SELECT id, payload FROM symbols WHERE id > ? ORDER BY id LIMIT ?`)
	x.selLookups = r.prep(`SELECT payload FROM lookups WHERE file_id = ? ORDER BY ordinal`)
	x.selIdentities = r.prep(`SELECT payload FROM identities WHERE file_id = ?`)
	x.selEntity = r.prep(`SELECT entity_id FROM entities WHERE file_id = ? AND declaration_id = ?`)
	x.selPrevious = r.prep(`SELECT found, payload FROM previous WHERE lineage = ?`)
	for i, n := range inBuckets {
		x.selSymbolsIn[i] = r.prep(`SELECT id, payload FROM symbols WHERE id IN (` + placeholders(n) + `)`)
	}
	if err = errors.Join(w.err, r.err); err != nil {
		return nil, err
	}
	return x, nil
}

// Close releases every connection and deletes the index files it created.
// It is idempotent; calls after Close fail with an error.
func (x *Index) Close() error {
	if x.closed.Swap(true) {
		return nil
	}
	var errs []error
	errs = append(errs, closeStmts(x.stmts), closeDB(x.readers), closeDB(x.writer))
	if x.root != nil {
		errs = append(errs, x.root.Close())
	}
	errs = append(errs, removeDB(x.path))
	return errors.Join(errs...)
}

func (x *Index) live() error {
	if x.closed.Load() {
		return errClosed
	}
	return nil
}

// writeTx runs fn inside the single write transaction slot.
func (x *Index) writeTx(ctx context.Context, fn func(*sql.Tx) error) error {
	x.wmu.Lock()
	defer x.wmu.Unlock()
	tx, err := x.writer.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	if err := fn(tx); err != nil {
		_ = tx.Rollback()
		return err
	}
	return tx.Commit()
}

// Build returns the evaluated build context for the commit.
func (x *Index) Build() bc.BuildContext { return x.build }

// --- inventory ---------------------------------------------------------------

type fileRow struct {
	id, lineage, module, set, path string
	affected                       int64
	source                         []byte
}

// PutFiles stores inventory entries in one transaction. An entry with the
// same FileID, or the same (source set, path), replaces the existing row, so
// the inventory may be written in batches. FileID and Lineage are required.
func (x *Index) PutFiles(ctx context.Context, files []semantic.SourceInput) error {
	if err := x.live(); err != nil {
		return err
	}
	rows := make([]fileRow, len(files))
	for i := range files {
		f := &files[i]
		if f.Source.FileID == "" || f.Lineage == "" {
			return fmt.Errorf("localindex: PutFiles: entry %d needs FileID and Lineage: %w", i, semantic.ErrInvalid)
		}
		src, err := json.Marshal(&f.Source)
		if err != nil {
			return err
		}
		rows[i] = fileRow{id: string(f.Source.FileID), lineage: f.Lineage, module: f.Source.ModuleID, set: f.Source.SourceSetID, path: f.Source.Path, source: src}
		if f.Affected {
			rows[i].affected = 1
		}
	}
	return x.writeTx(ctx, func(tx *sql.Tx) error {
		st := tx.StmtContext(ctx, x.insFile)
		for i := range rows {
			r := &rows[i]
			if _, err := st.ExecContext(ctx, r.id, r.lineage, r.module, r.set, r.path, r.affected, r.source); err != nil {
				return err
			}
		}
		return nil
	})
}

// SetAffected marks inventory entries Affected=true. An unknown ID fails the
// whole call with semantic.ErrNotFound and changes nothing.
func (x *Index) SetAffected(ctx context.Context, fileIDs []ir.FileID) error {
	if err := x.live(); err != nil {
		return err
	}
	return x.writeTx(ctx, func(tx *sql.Tx) error {
		st := tx.StmtContext(ctx, x.updAffected)
		for _, id := range fileIDs {
			res, err := st.ExecContext(ctx, string(id))
			if err != nil {
				return err
			}
			n, err := res.RowsAffected()
			if err != nil {
				return err
			}
			if n == 0 {
				return fmt.Errorf("localindex: SetAffected %q: %w", id, semantic.ErrNotFound)
			}
		}
		return nil
	})
}

func scanFile(rows *sql.Rows) (semantic.SourceInput, error) {
	var in semantic.SourceInput
	var affected int64
	var raw sql.RawBytes
	if err := rows.Scan(&in.Lineage, &affected, &raw); err != nil {
		return in, err
	}
	if err := json.Unmarshal(raw, &in.Source); err != nil {
		return in, fmt.Errorf("localindex: decode file: %w", err)
	}
	in.Affected = affected != 0
	return in, nil
}

func (x *Index) oneFile(ctx context.Context, st *sql.Stmt, args ...any) (semantic.SourceInput, error) {
	rows, err := st.QueryContext(ctx, args...)
	if err != nil {
		return semantic.SourceInput{}, err
	}
	defer rows.Close()
	if !rows.Next() {
		if err := rows.Err(); err != nil {
			return semantic.SourceInput{}, err
		}
		return semantic.SourceInput{}, semantic.ErrNotFound
	}
	return scanFile(rows)
}

// Files lists the inventory ordered by module, source set and path.
func (x *Index) Files(ctx context.Context) ([]semantic.SourceInput, error) {
	if err := x.live(); err != nil {
		return nil, err
	}
	rows, err := x.selFiles.QueryContext(ctx)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []semantic.SourceInput
	for rows.Next() {
		in, err := scanFile(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, in)
	}
	return out, rows.Err()
}

// File resolves an inventory entry by file ID.
func (x *Index) File(ctx context.Context, id ir.FileID) (semantic.SourceInput, error) {
	if err := x.live(); err != nil {
		return semantic.SourceInput{}, err
	}
	in, err := x.oneFile(ctx, x.selFile, string(id))
	if err != nil {
		return in, fmt.Errorf("localindex: file %q: %w", id, err)
	}
	return in, nil
}

// FileByPath resolves the repository-relative path within one source set.
func (x *Index) FileByPath(ctx context.Context, sourceSetID bc.SourceSetID, p string) (semantic.SourceInput, error) {
	if err := x.live(); err != nil {
		return semantic.SourceInput{}, err
	}
	in, err := x.oneFile(ctx, x.selFileByPath, string(sourceSetID), p)
	if err != nil {
		return in, fmt.Errorf("localindex: file %s:%s: %w", sourceSetID, p, err)
	}
	return in, nil
}

// --- syntax and source bytes -------------------------------------------------

// Syntax returns the parsed IR of in from the syntax cache, keyed by content
// hash and this run's descriptor digest. A small LRU of decoded files serves
// the repeated reads a caller makes while working on one file.
func (x *Index) Syntax(ctx context.Context, in semantic.SourceInput) (ir.SourceFile, error) {
	if err := x.live(); err != nil {
		return ir.SourceFile{}, err
	}
	key := in.Source.ContentSHA256
	if key == "" {
		return ir.SourceFile{}, fmt.Errorf("localindex: Syntax: empty content hash: %w", semantic.ErrInvalid)
	}
	profile := SyntaxKey(x.digest, x.sets[in.Source.SourceSetID], in)
	lruKey := key + "\x00" + profile
	if f, ok := x.syntaxLRU.get(lruKey); ok {
		f.Source = in.Source
		return f, nil
	}
	if x.syntax == nil {
		return ir.SourceFile{}, fmt.Errorf("localindex: syntax %s: %w", key, semantic.ErrNotFound)
	}
	f, ok, err := x.syntax.Get(ctx, key, profile)
	if err != nil {
		return ir.SourceFile{}, err
	}
	if !ok {
		return ir.SourceFile{}, fmt.Errorf("localindex: syntax %s: %w", key, semantic.ErrNotFound)
	}
	x.syntaxLRU.put(lruKey, f)
	// The same bytes may be parsed in several compilation contexts; the cached
	// IR is rebound to the identity of the variant being read.
	f.Source = in.Source
	return f, nil
}

// SyntaxKey is the descriptor a file's IR is cached under: the parser
// descriptor digest plus the file's syntax profile (language, language
// version and the source set's preview flag and options). The parse stage
// and the index must derive it identically.
func SyntaxKey(descriptorDigest string, set bc.SourceSet, in semantic.SourceInput) string {
	b, _ := json.Marshal(struct {
		Descriptor, Language, Version string
		Preview                       bool
		Settings                      map[string]string
	}{descriptorDigest, in.Source.Language, in.Source.LanguageVersion, set.EnablePreview, set.LanguageOptions})
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// SourceBytes reads in.Source.Path under the checkout root and verifies the
// size and SHA-256 recorded in the inventory. A path that is not a clean
// relative path is ErrInvalid; a symlink escaping the checkout, or bytes that
// do not match the inventory, is ErrIntegrity; a missing file is ErrNotFound.
func (x *Index) SourceBytes(ctx context.Context, in semantic.SourceInput) ([]byte, error) {
	if err := x.live(); err != nil {
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	p := in.Source.Path
	if p == "" || p == "." || path.IsAbs(p) || path.Clean(p) != p || hasDotDot(p) {
		return nil, fmt.Errorf("localindex: path %q: %w", p, semantic.ErrInvalid)
	}
	f, err := x.root.Open(filepath.FromSlash(p))
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil, fmt.Errorf("localindex: %s: %w", p, semantic.ErrNotFound)
		}
		return nil, fmt.Errorf("localindex: %s: %w: %v", p, semantic.ErrIntegrity, err)
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, fmt.Errorf("localindex: %s is not a regular file: %w", p, semantic.ErrInvalid)
	}
	if uint64(info.Size()) != in.Source.SizeBytes {
		return nil, fmt.Errorf("localindex: %s: size %d, inventory %d: %w", p, info.Size(), in.Source.SizeBytes, semantic.ErrIntegrity)
	}
	data := make([]byte, info.Size())
	if _, err := io.ReadFull(f, data); err != nil {
		return nil, fmt.Errorf("localindex: %s: %w: %v", p, semantic.ErrIntegrity, err)
	}
	var extra [1]byte
	if n, err := f.Read(extra[:]); n != 0 || err != io.EOF {
		return nil, fmt.Errorf("localindex: %s: grew after stat: %w", p, semantic.ErrIntegrity)
	}
	sum := sha256.Sum256(data)
	if !strings.EqualFold(hex.EncodeToString(sum[:]), in.Source.ContentSHA256) {
		return nil, fmt.Errorf("localindex: %s: content hash mismatch: %w", p, semantic.ErrIntegrity)
	}
	return data, nil
}

func hasDotDot(p string) bool {
	for _, seg := range strings.Split(p, "/") {
		if seg == ".." {
			return true
		}
	}
	return false
}

// --- symbols -----------------------------------------------------------------

// PutSymbols writes symbols in one transaction; an existing ID is replaced.
func (x *Index) PutSymbols(ctx context.Context, symbols []semantic.Symbol) error {
	if err := x.live(); err != nil {
		return err
	}
	payloads := make([][]byte, len(symbols))
	for i := range symbols {
		if symbols[i].ID == "" {
			return fmt.Errorf("localindex: PutSymbols: symbol %d has no ID: %w", i, semantic.ErrInvalid)
		}
		p, err := json.Marshal(&symbols[i])
		if err != nil {
			return err
		}
		payloads[i] = p
	}
	return x.writeTx(ctx, func(tx *sql.Tx) error {
		st := tx.StmtContext(ctx, x.insSymbol)
		for i := range symbols {
			fileID := ""
			if s := symbols[i].Source; s != nil {
				fileID = string(s.FileID)
			}
			if _, err := st.ExecContext(ctx, symbols[i].ID, fileID, payloads[i]); err != nil {
				return err
			}
		}
		return nil
	})
}

// Symbol is a point read by ID.
func (x *Index) Symbol(ctx context.Context, id string) (semantic.Symbol, error) {
	var s semantic.Symbol
	if err := x.live(); err != nil {
		return s, err
	}
	if err := queryJSON(ctx, x.selSymbol, &s, id); err != nil {
		return semantic.Symbol{}, fmt.Errorf("localindex: symbol %q: %w", id, err)
	}
	return s, nil
}

// Symbols reads many IDs with prepared IN statements of at most 500 IDs.
// Unknown IDs are simply absent from the result.
func (x *Index) Symbols(ctx context.Context, ids []string) (map[string]semantic.Symbol, error) {
	if err := x.live(); err != nil {
		return nil, err
	}
	out := make(map[string]semantic.Symbol, len(ids))
	args := make([]any, 0, maxIn)
	for start := 0; start < len(ids); start += maxIn {
		chunk := ids[start:min(start+maxIn, len(ids))]
		bucket := 0
		for inBuckets[bucket] < len(chunk) {
			bucket++
		}
		args = args[:0]
		for _, id := range chunk {
			args = append(args, id)
		}
		for len(args) < inBuckets[bucket] {
			args = append(args, chunk[len(chunk)-1])
		}
		if err := x.collectSymbols(ctx, x.selSymbolsIn[bucket], args, func(id string, s semantic.Symbol) { out[id] = s }); err != nil {
			return nil, err
		}
	}
	return out, nil
}

func (x *Index) collectSymbols(ctx context.Context, st *sql.Stmt, args []any, visit func(string, semantic.Symbol)) error {
	rows, err := st.QueryContext(ctx, args...)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var id string
		var raw sql.RawBytes
		if err := rows.Scan(&id, &raw); err != nil {
			return err
		}
		var s semantic.Symbol
		if err := json.Unmarshal(raw, &s); err != nil {
			return fmt.Errorf("localindex: decode symbol %q: %w", id, err)
		}
		visit(id, s)
	}
	return rows.Err()
}

// SymbolsByFile returns the source symbols declared in one file, by ID.
func (x *Index) SymbolsByFile(ctx context.Context, id ir.FileID) ([]semantic.Symbol, error) {
	if err := x.live(); err != nil {
		return nil, err
	}
	rows, err := x.selSymbolsByFile.QueryContext(ctx, string(id))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []semantic.Symbol
	for rows.Next() {
		var raw sql.RawBytes
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		var s semantic.Symbol
		if err := json.Unmarshal(raw, &s); err != nil {
			return nil, fmt.Errorf("localindex: decode symbol: %w", err)
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

// EachSymbol visits every symbol in ID order. It reads pages of 1024 rows by
// keyset (id > last), so no read connection or WAL snapshot is held while fn
// runs; fn may itself read from the index. Each ID is visited at most once.
func (x *Index) EachSymbol(ctx context.Context, fn func(semantic.Symbol) error) error {
	if err := x.live(); err != nil {
		return err
	}
	page := make([]semantic.Symbol, 0, symbolPage)
	var last string
	first := true
	for {
		page = page[:0]
		var err error
		visit := func(id string, s semantic.Symbol) { page = append(page, s); last = id }
		if first {
			err = x.collectSymbols(ctx, x.selSymbolsFirst, []any{symbolPage}, visit)
		} else {
			err = x.collectSymbols(ctx, x.selSymbolsAfter, []any{last, symbolPage}, visit)
		}
		if err != nil {
			return err
		}
		for i := range page {
			if err := fn(page[i]); err != nil {
				return err
			}
		}
		if len(page) < symbolPage {
			return nil
		}
		first = false
	}
}

// --- lookups -----------------------------------------------------------------

// PutLookups replaces the lookups of one file in one transaction.
func (x *Index) PutLookups(ctx context.Context, id ir.FileID, lookups []semantic.Lookup) error {
	if err := x.live(); err != nil {
		return err
	}
	payloads := make([][]byte, len(lookups))
	for i := range lookups {
		p, err := json.Marshal(&lookups[i])
		if err != nil {
			return err
		}
		payloads[i] = p
	}
	fileID := string(id)
	return x.writeTx(ctx, func(tx *sql.Tx) error {
		if _, err := tx.StmtContext(ctx, x.delLookups).ExecContext(ctx, fileID); err != nil {
			return err
		}
		st := tx.StmtContext(ctx, x.insLookup)
		for i := range payloads {
			if _, err := st.ExecContext(ctx, fileID, i, payloads[i]); err != nil {
				return err
			}
		}
		return nil
	})
}

// Lookups returns a file's lookups in the order they were written.
func (x *Index) Lookups(ctx context.Context, id ir.FileID) ([]semantic.Lookup, error) {
	if err := x.live(); err != nil {
		return nil, err
	}
	rows, err := x.selLookups.QueryContext(ctx, string(id))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []semantic.Lookup
	for rows.Next() {
		var raw sql.RawBytes
		if err := rows.Scan(&raw); err != nil {
			return nil, err
		}
		var l semantic.Lookup
		if err := json.Unmarshal(raw, &l); err != nil {
			return nil, fmt.Errorf("localindex: decode lookup: %w", err)
		}
		out = append(out, l)
	}
	return out, rows.Err()
}

// --- identities --------------------------------------------------------------

// PutIdentities stores a file's identity map and rebuilds its
// (declaration -> entity) rows in one transaction.
func (x *Index) PutIdentities(ctx context.Context, f semantic.FileIdentities) error {
	if err := x.live(); err != nil {
		return err
	}
	if f.FileID == "" {
		return fmt.Errorf("localindex: PutIdentities: empty file ID: %w", semantic.ErrInvalid)
	}
	payload, err := json.Marshal(&f)
	if err != nil {
		return err
	}
	fileID := string(f.FileID)
	return x.writeTx(ctx, func(tx *sql.Tx) error {
		if _, err := tx.StmtContext(ctx, x.insIdentities).ExecContext(ctx, fileID, f.Lineage, payload); err != nil {
			return err
		}
		if _, err := tx.StmtContext(ctx, x.delEntities).ExecContext(ctx, fileID); err != nil {
			return err
		}
		st := tx.StmtContext(ctx, x.insEntity)
		for i := range f.Declarations {
			d := &f.Declarations[i]
			if _, err := st.ExecContext(ctx, fileID, string(d.DeclarationID), d.EntityID); err != nil {
				return err
			}
		}
		return nil
	})
}

// Identities returns the identity map written for a file in this run.
func (x *Index) Identities(ctx context.Context, id ir.FileID) (semantic.FileIdentities, error) {
	var f semantic.FileIdentities
	if err := x.live(); err != nil {
		return f, err
	}
	if err := queryJSON(ctx, x.selIdentities, &f, string(id)); err != nil {
		return semantic.FileIdentities{}, fmt.Errorf("localindex: identities %q: %w", id, err)
	}
	return f, nil
}

// AliasPrevious makes PreviousIdentities(newLineage) read oldLineage from the
// previous generation, for a file renamed in this commit.
func (x *Index) AliasPrevious(newLineage, oldLineage string) {
	x.aliasMu.Lock()
	defer x.aliasMu.Unlock()
	x.aliases[newLineage] = oldLineage
}

func (x *Index) resolveAlias(lineage string) string {
	x.aliasMu.RLock()
	defer x.aliasMu.RUnlock()
	for range 16 {
		old, ok := x.aliases[lineage]
		if !ok || old == lineage {
			break
		}
		lineage = old
	}
	return lineage
}

// previousEntry is a cached baseline map; missing records an ErrNotFound so
// a new file does not hit the previous generation on every lookup.
type previousEntry struct {
	ids     semantic.FileIdentities
	missing bool
}

func (e previousEntry) result(lineage string) (semantic.FileIdentities, error) {
	if e.missing {
		return semantic.FileIdentities{}, fmt.Errorf("localindex: previous identities %q: %w", lineage, semantic.ErrNotFound)
	}
	return e.ids, nil
}

// PreviousIdentities returns the baseline identity map for a lineage
// (following rename aliases). Maps are fetched once from the previous
// reader, kept in the previous table for the run, and decoded into a
// bounded LRU. Concurrent misses for one lineage share a single fetch.
func (x *Index) PreviousIdentities(ctx context.Context, lineage string) (semantic.FileIdentities, error) {
	if err := x.live(); err != nil {
		return semantic.FileIdentities{}, err
	}
	lineage = x.resolveAlias(lineage)
	if e, ok := x.prevLRU.get(lineage); ok {
		return e.result(lineage)
	}
	v, err, _ := x.prevFlight.Do(lineage, func() (any, error) {
		e, err := x.loadPrevious(ctx, lineage)
		if err != nil {
			return nil, err
		}
		x.prevLRU.put(lineage, e)
		return e, nil
	})
	if err != nil {
		return semantic.FileIdentities{}, err
	}
	return v.(previousEntry).result(lineage)
}

func (x *Index) loadPrevious(ctx context.Context, lineage string) (previousEntry, error) {
	e, found, err := x.storedPrevious(ctx, lineage)
	if err != nil || found {
		return e, err
	}
	if x.previous == nil {
		e = previousEntry{missing: true}
	} else {
		ids, err := x.previous.PreviousIdentities(ctx, lineage)
		switch {
		case err == nil:
			e = previousEntry{ids: ids}
		case errors.Is(err, semantic.ErrNotFound):
			e = previousEntry{missing: true}
		default:
			return previousEntry{}, err // transient: not cached
		}
	}
	var payload []byte
	foundFlag := int64(1)
	if e.missing {
		foundFlag = 0
	} else if payload, err = json.Marshal(&e.ids); err != nil {
		return previousEntry{}, err
	}
	err = x.writeTx(ctx, func(tx *sql.Tx) error {
		_, err := tx.StmtContext(ctx, x.insPrevious).ExecContext(ctx, lineage, foundFlag, payload)
		return err
	})
	return e, err
}

func (x *Index) storedPrevious(ctx context.Context, lineage string) (previousEntry, bool, error) {
	rows, err := x.selPrevious.QueryContext(ctx, lineage)
	if err != nil {
		return previousEntry{}, false, err
	}
	defer rows.Close()
	if !rows.Next() {
		return previousEntry{}, false, rows.Err()
	}
	var found int64
	var raw sql.RawBytes
	if err := rows.Scan(&found, &raw); err != nil {
		return previousEntry{}, false, err
	}
	if found == 0 {
		return previousEntry{missing: true}, true, nil
	}
	var e previousEntry
	if err := json.Unmarshal(raw, &e.ids); err != nil {
		return previousEntry{}, false, fmt.Errorf("localindex: decode previous %q: %w", lineage, err)
	}
	return e, true, nil
}

// Entity resolves (file, declaration) to its entity ID: from this run's
// identities when the file was matched here, otherwise from the previous
// generation's map for the file's lineage.
func (x *Index) Entity(ctx context.Context, id ir.FileID, declaration ir.DeclarationID) (string, error) {
	if err := x.live(); err != nil {
		return "", err
	}
	rows, err := x.selEntity.QueryContext(ctx, string(id), string(declaration))
	if err != nil {
		return "", err
	}
	if rows.Next() {
		var entity string
		err := rows.Scan(&entity)
		_ = rows.Close()
		return entity, err
	}
	err = rows.Err()
	_ = rows.Close()
	if err != nil {
		return "", err
	}
	in, err := x.File(ctx, id)
	if err != nil {
		return "", err
	}
	prev, err := x.PreviousIdentities(ctx, in.Lineage)
	if err != nil {
		return "", err
	}
	d, ok := prev.Declaration(declaration)
	if !ok {
		return "", fmt.Errorf("localindex: entity %s/%s: %w", id, declaration, semantic.ErrNotFound)
	}
	return d.EntityID, nil
}
