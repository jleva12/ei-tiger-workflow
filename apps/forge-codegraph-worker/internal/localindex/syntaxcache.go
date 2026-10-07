package localindex

import (
	"bytes"
	"compress/gzip"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"time"

	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

// SyntaxCache is the persistent, cross-run cache of parsed IR, keyed by the
// source content hash and the digest of the parser descriptor that produced
// it. It lives at <dir>/syntax.sqlite in WAL mode with synchronous=NORMAL:
// the file is always consistent and a crash loses at most the latest puts,
// which are recomputable. Entries are gzip-compressed JSON. When the sum of
// stored payload bytes exceeds maxBytes, least-recently-used entries are
// evicted on the next Put. Recency is refreshed on Get at most once per
// minute per entry, so hot reads do not each cost a write.
//
// One process owns a cache directory at a time (the byte total is tracked in
// memory). Safe for concurrent use.
type SyntaxCache struct {
	path     string
	maxBytes int64
	writer   *sql.DB
	readers  *sql.DB
	stmts    []*sql.Stmt

	selGet, selSize, insPut, updTouch, delEntry, selOldest *sql.Stmt

	mu        sync.Mutex // serializes writes; guards total and lastStamp
	total     int64
	lastStamp int64
	now       func() int64
	closed    atomic.Bool
}

const (
	syntaxFile    = "syntax.sqlite"
	touchInterval = int64(time.Minute)
	evictBatch    = 64
)

var errCacheClosed = errors.New("localindex: syntax cache is closed")

const syntaxSchema = `
CREATE TABLE IF NOT EXISTS syntax (
	content_sha256    TEXT    NOT NULL,
	descriptor_digest TEXT    NOT NULL,
	size              INTEGER NOT NULL,
	last_used         INTEGER NOT NULL,
	payload           BLOB    NOT NULL,
	PRIMARY KEY (content_sha256, descriptor_digest)
);
CREATE INDEX IF NOT EXISTS syntax_lru ON syntax(last_used);
`

func cachePragmas(writer bool) []string {
	p := []string{
		"PRAGMA synchronous=NORMAL",
		"PRAGMA temp_store=MEMORY",
		"PRAGMA cache_size=-65536",
		"PRAGMA mmap_size=1073741824",
		"PRAGMA foreign_keys=OFF",
	}
	if writer {
		return append([]string{"PRAGMA journal_mode=WAL"}, p...)
	}
	return append(p, "PRAGMA query_only=ON")
}

// OpenSyntaxCache opens or creates <dir>/syntax.sqlite. maxBytes bounds the
// stored payload bytes; a value <= 0 disables eviction.
func OpenSyntaxCache(dir string, maxBytes int64) (_ *SyntaxCache, err error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	ctx := context.Background()
	c := &SyntaxCache{path: filepath.Join(dir, syntaxFile), maxBytes: maxBytes, now: func() int64 { return time.Now().UnixNano() }}
	defer func() {
		if err != nil {
			_ = c.Close()
		}
	}()
	c.writer = openPool(newConnector(c.path, "immediate", cachePragmas(true)), 1)
	if _, err = c.writer.ExecContext(ctx, syntaxSchema); err != nil {
		return nil, fmt.Errorf("localindex: syntax cache schema: %w", err)
	}
	if err = c.writer.QueryRowContext(ctx, `SELECT COALESCE(SUM(size), 0) FROM syntax`).Scan(&c.total); err != nil {
		return nil, fmt.Errorf("localindex: syntax cache size: %w", err)
	}
	c.readers = openPool(newConnector(c.path, "", cachePragmas(false)), readConns())

	w := &preparer{ctx: ctx, db: c.writer, all: &c.stmts}
	c.selSize = w.prep(`SELECT size FROM syntax WHERE content_sha256 = ? AND descriptor_digest = ?`)
	c.insPut = w.prep(`INSERT OR REPLACE INTO syntax(content_sha256, descriptor_digest, size, last_used, payload) VALUES (?, ?, ?, ?, ?)`)
	c.updTouch = w.prep(`UPDATE syntax SET last_used = ? WHERE content_sha256 = ? AND descriptor_digest = ?`)
	c.delEntry = w.prep(`DELETE FROM syntax WHERE content_sha256 = ? AND descriptor_digest = ?`)
	c.selOldest = w.prep(`SELECT content_sha256, descriptor_digest, size FROM syntax ORDER BY last_used LIMIT ?`)
	r := &preparer{ctx: ctx, db: c.readers, all: &c.stmts}
	c.selGet = r.prep(`SELECT payload, size, last_used FROM syntax WHERE content_sha256 = ? AND descriptor_digest = ?`)
	if err = errors.Join(w.err, r.err); err != nil {
		return nil, err
	}
	return c, nil
}

// Close releases the connections. The cache file is kept for the next run.
func (c *SyntaxCache) Close() error {
	if c.closed.Swap(true) {
		return nil
	}
	return errors.Join(closeStmts(c.stmts), closeDB(c.readers), closeDB(c.writer))
}

// Get returns the cached IR for the (content hash, descriptor digest) pair.
// A payload that no longer decodes is deleted and reported as a miss.
func (c *SyntaxCache) Get(ctx context.Context, contentSHA256, descriptorDigest string) (ir.SourceFile, bool, error) {
	if c.closed.Load() {
		return ir.SourceFile{}, false, errCacheClosed
	}
	rows, err := c.selGet.QueryContext(ctx, contentSHA256, descriptorDigest)
	if err != nil {
		return ir.SourceFile{}, false, err
	}
	if !rows.Next() {
		err := rows.Err()
		_ = rows.Close()
		return ir.SourceFile{}, false, err
	}
	var raw sql.RawBytes
	var size, lastUsed int64
	if err := rows.Scan(&raw, &size, &lastUsed); err != nil {
		_ = rows.Close()
		return ir.SourceFile{}, false, err
	}
	// Copy and release the connection before the (comparatively slow) decode.
	payload := bytes.Clone(raw)
	if err := rows.Close(); err != nil {
		return ir.SourceFile{}, false, err
	}
	f, err := decodeSyntax(payload)
	if err != nil {
		if derr := c.remove(ctx, contentSHA256, descriptorDigest, size); derr != nil {
			return ir.SourceFile{}, false, derr
		}
		return ir.SourceFile{}, false, nil
	}
	if c.now()-lastUsed > touchInterval {
		if err := c.touch(ctx, contentSHA256, descriptorDigest); err != nil {
			return ir.SourceFile{}, false, err
		}
	}
	return f, true, nil
}

// Put stores f under the pair, replacing any previous entry, then evicts the
// least recently used entries until the cache is within maxBytes.
func (c *SyntaxCache) Put(ctx context.Context, contentSHA256, descriptorDigest string, f ir.SourceFile) error {
	if c.closed.Load() {
		return errCacheClosed
	}
	if contentSHA256 == "" || descriptorDigest == "" {
		return fmt.Errorf("localindex: syntax cache key: %w", semantic.ErrInvalid)
	}
	payload, err := encodeSyntax(&f)
	if err != nil {
		return fmt.Errorf("localindex: encode syntax: %w", err)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	tx, err := c.writer.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	total, err := c.putLocked(ctx, tx, contentSHA256, descriptorDigest, payload)
	if err != nil {
		_ = tx.Rollback()
		return err
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	c.total = total
	return nil
}

func (c *SyntaxCache) putLocked(ctx context.Context, tx *sql.Tx, sha, digest string, payload []byte) (int64, error) {
	var old int64
	if err := tx.StmtContext(ctx, c.selSize).QueryRowContext(ctx, sha, digest).Scan(&old); err != nil && !errors.Is(err, sql.ErrNoRows) {
		return 0, err
	}
	size := int64(len(payload))
	if _, err := tx.StmtContext(ctx, c.insPut).ExecContext(ctx, sha, digest, size, c.stamp(), payload); err != nil {
		return 0, err
	}
	total := c.total - old + size
	if c.maxBytes <= 0 {
		return total, nil
	}
	for total > c.maxBytes {
		victims, err := c.oldest(ctx, tx, evictBatch)
		if err != nil {
			return 0, err
		}
		if len(victims) == 0 {
			break
		}
		del := tx.StmtContext(ctx, c.delEntry)
		for _, v := range victims {
			if total <= c.maxBytes {
				break
			}
			if _, err := del.ExecContext(ctx, v.sha, v.digest); err != nil {
				return 0, err
			}
			total -= v.size
		}
	}
	return total, nil
}

type cacheVictim struct {
	sha, digest string
	size        int64
}

func (c *SyntaxCache) oldest(ctx context.Context, tx *sql.Tx, limit int) ([]cacheVictim, error) {
	rows, err := tx.StmtContext(ctx, c.selOldest).QueryContext(ctx, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []cacheVictim
	for rows.Next() {
		var v cacheVictim
		if err := rows.Scan(&v.sha, &v.digest, &v.size); err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

// stamp returns a strictly increasing recency value; caller holds mu.
func (c *SyntaxCache) stamp() int64 {
	n := c.now()
	if n <= c.lastStamp {
		n = c.lastStamp + 1
	}
	c.lastStamp = n
	return n
}

func (c *SyntaxCache) touch(ctx context.Context, sha, digest string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	_, err := c.updTouch.ExecContext(ctx, c.stamp(), sha, digest)
	return err
}

func (c *SyntaxCache) remove(ctx context.Context, sha, digest string, size int64) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	res, err := c.delEntry.ExecContext(ctx, sha, digest)
	if err != nil {
		return err
	}
	if n, err := res.RowsAffected(); err == nil && n > 0 {
		c.total -= size
	}
	return nil
}

var gzipWriters = sync.Pool{New: func() any {
	w, _ := gzip.NewWriterLevel(io.Discard, gzip.BestSpeed)
	return w
}}

func encodeSyntax(f *ir.SourceFile) ([]byte, error) {
	var buf bytes.Buffer
	w := gzipWriters.Get().(*gzip.Writer)
	defer gzipWriters.Put(w)
	w.Reset(&buf)
	if err := json.NewEncoder(w).Encode(f); err != nil {
		return nil, err
	}
	if err := w.Close(); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func decodeSyntax(payload []byte) (ir.SourceFile, error) {
	r, err := gzip.NewReader(bytes.NewReader(payload))
	if err != nil {
		return ir.SourceFile{}, err
	}
	// ReadAll drains to EOF so the gzip CRC is checked before decoding.
	data, err := io.ReadAll(r)
	if err != nil {
		return ir.SourceFile{}, err
	}
	var f ir.SourceFile
	if err := json.Unmarshal(data, &f); err != nil {
		return ir.SourceFile{}, err
	}
	return f, nil
}
