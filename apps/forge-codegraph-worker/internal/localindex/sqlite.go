package localindex

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"runtime"
	"strings"

	"github.com/mattn/go-sqlite3"

	"ei-aitiger-codegraph/pkg/semantic"
)

// connector opens SQLite connections and applies per-connection pragmas
// before database/sql pools them. go-sqlite3 exposes DSN switches for only a
// few pragmas, so every setting is applied uniformly here, once per
// connection, and never again on the hot path.
type connector struct {
	dsn     string
	pragmas []string
	drv     *sqlite3.SQLiteDriver
}

// newConnector builds a file: URI DSN so paths with spaces or '?' survive.
// txlock is the go-sqlite3 transaction mode ("immediate" for writers).
func newConnector(path, txlock string, pragmas []string) *connector {
	q := url.Values{}
	q.Set("_busy_timeout", "10000")
	if txlock != "" {
		q.Set("_txlock", txlock)
	}
	u := url.URL{Scheme: "file", Path: path, RawQuery: q.Encode()}
	return &connector{dsn: u.String(), pragmas: pragmas, drv: &sqlite3.SQLiteDriver{}}
}

func (c *connector) Connect(ctx context.Context) (driver.Conn, error) {
	conn, err := c.drv.Open(c.dsn)
	if err != nil {
		return nil, err
	}
	sc, ok := conn.(*sqlite3.SQLiteConn)
	if !ok {
		_ = conn.Close()
		return nil, errors.New("localindex: unexpected sqlite driver connection type")
	}
	for _, p := range c.pragmas {
		if _, err := sc.ExecContext(ctx, p, nil); err != nil {
			_ = conn.Close()
			return nil, fmt.Errorf("localindex: %s: %w", p, err)
		}
	}
	return conn, nil
}

func (c *connector) Driver() driver.Driver { return c.drv }

// openPool returns a pool of exactly n long-lived connections. Connections
// are never recycled, so statements prepared on them stay prepared.
func openPool(c *connector, n int) *sql.DB {
	db := sql.OpenDB(c)
	db.SetMaxOpenConns(n)
	db.SetMaxIdleConns(n)
	db.SetConnMaxLifetime(0)
	db.SetConnMaxIdleTime(0)
	return db
}

// readConns sizes a read pool: enough for one goroutine per CPU plus a few
// nested reads from streaming callbacks, capped so page caches stay bounded.
func readConns() int { return min(max(runtime.GOMAXPROCS(0), 4), 16) }

// preparer prepares statements against one pool, recording the first error
// and every statement so Close can release them.
type preparer struct {
	ctx context.Context
	db  *sql.DB
	all *[]*sql.Stmt
	err error
}

func (p *preparer) prep(query string) *sql.Stmt {
	if p.err != nil {
		return nil
	}
	st, err := p.db.PrepareContext(p.ctx, query)
	if err != nil {
		p.err = fmt.Errorf("localindex: prepare %q: %w", query, err)
		return nil
	}
	*p.all = append(*p.all, st)
	return st
}

func closeStmts(all []*sql.Stmt) error {
	var errs []error
	for _, s := range all {
		if s != nil {
			errs = append(errs, s.Close())
		}
	}
	return errors.Join(errs...)
}

func closeDB(db *sql.DB) error {
	if db == nil {
		return nil
	}
	return db.Close()
}

// removeDB deletes a SQLite database and its side files; absent files are fine.
func removeDB(path string) error {
	var errs []error
	for _, suffix := range []string{"", "-wal", "-shm", "-journal"} {
		if err := os.Remove(path + suffix); err != nil && !errors.Is(err, os.ErrNotExist) {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

func placeholders(n int) string {
	return strings.TrimSuffix(strings.Repeat("?,", n), ",")
}

// queryJSON runs a single-row statement whose only column is a JSON payload
// and decodes it into dest. No row is semantic.ErrNotFound. The payload is
// scanned as RawBytes and decoded before the cursor closes, so it is copied
// exactly once (by the driver).
func queryJSON(ctx context.Context, st *sql.Stmt, dest any, args ...any) error {
	rows, err := st.QueryContext(ctx, args...)
	if err != nil {
		return err
	}
	defer rows.Close()
	if !rows.Next() {
		if err := rows.Err(); err != nil {
			return err
		}
		return semantic.ErrNotFound
	}
	var raw sql.RawBytes
	if err := rows.Scan(&raw); err != nil {
		return err
	}
	if err := json.Unmarshal(raw, dest); err != nil {
		return fmt.Errorf("localindex: decode row: %w", err)
	}
	return nil
}
