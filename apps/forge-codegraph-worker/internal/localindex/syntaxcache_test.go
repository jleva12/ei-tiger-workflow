package localindex

import (
	"bytes"
	"compress/gzip"
	"context"
	"errors"
	"path/filepath"
	"reflect"
	"strconv"
	"testing"
	"time"

	"golang.org/x/sync/errgroup"

	"ei-aitiger-codegraph/pkg/semantic"
)

func TestSyntaxCacheHitMissReopen(t *testing.T) {
	ctx := context.Background()
	dir := filepath.Join(t.TempDir(), "cache")
	c, err := OpenSyntaxCache(dir, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	sha := shaOf([]byte("content"))
	if _, ok, err := c.Get(ctx, sha, "d1"); err != nil || ok {
		t.Fatalf("Get on empty cache = %v, %v", ok, err)
	}
	want := sampleIR("f", sha, "A")
	if err := c.Put(ctx, sha, "d1", want); err != nil {
		t.Fatal(err)
	}
	got, ok, err := c.Get(ctx, sha, "d1")
	if err != nil || !ok || !reflect.DeepEqual(got, want) {
		t.Fatalf("Get = %+v, %v, %v", got, ok, err)
	}
	if _, ok, err := c.Get(ctx, sha, "d2"); err != nil || ok {
		t.Fatalf("Get other digest = %v, %v", ok, err)
	}
	if err := c.Put(ctx, "", "d1", want); !errors.Is(err, semantic.ErrInvalid) {
		t.Fatalf("Put empty key err = %v", err)
	}
	if err := c.Close(); err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.Get(ctx, sha, "d1"); err == nil {
		t.Fatal("Get after Close must fail")
	}
	c, err = OpenSyntaxCache(dir, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	got, ok, err = c.Get(ctx, sha, "d1")
	if err != nil || !ok || !reflect.DeepEqual(got, want) {
		t.Fatalf("Get after reopen = %+v, %v, %v", got, ok, err)
	}
	if c.total <= 0 {
		t.Fatalf("total after reopen = %d", c.total)
	}
}

func TestSyntaxCacheEviction(t *testing.T) {
	ctx := context.Background()
	dir := filepath.Join(t.TempDir(), "cache")
	probe, err := OpenSyntaxCache(dir, 0)
	if err != nil {
		t.Fatal(err)
	}
	entry := func(name string) (string, func() error) {
		sha := shaOf([]byte(name))
		return sha, func() error { return probe.Put(ctx, sha, "d", sampleIR(name, sha, "N"+name)) }
	}
	shaA, putA := entry("A")
	if err := putA(); err != nil {
		t.Fatal(err)
	}
	unit := probe.total // every entry is about this size
	if unit <= 0 {
		t.Fatalf("unit size = %d", unit)
	}
	if err := probe.Close(); err != nil {
		t.Fatal(err)
	}

	c, err := OpenSyntaxCache(dir, 2*unit+unit/2)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	clock := time.Now().UnixNano()
	c.now = func() int64 { return clock }
	probe = c
	shaB, putB := entry("B")
	shaC, putC := entry("C")
	shaD, putD := entry("D")
	shaE, putE := entry("E")
	if err := putB(); err != nil {
		t.Fatal(err)
	}
	if err := putC(); err != nil { // exceeds the budget: A (oldest) goes
		t.Fatal(err)
	}
	hit := func(sha string) bool {
		t.Helper()
		_, ok, err := c.Get(ctx, sha, "d")
		if err != nil {
			t.Fatal(err)
		}
		return ok
	}
	if hit(shaA) || !hit(shaB) || !hit(shaC) {
		t.Fatalf("after C: A=%v B=%v C=%v", hit(shaA), hit(shaB), hit(shaC))
	}
	if c.total > c.maxBytes {
		t.Fatalf("total %d exceeds budget %d", c.total, c.maxBytes)
	}
	// A read older than the touch interval refreshes recency, so B survives
	// the next eviction and C goes instead.
	clock += 2 * touchInterval
	if !hit(shaB) {
		t.Fatal("B lost")
	}
	if err := putD(); err != nil {
		t.Fatal(err)
	}
	if !hit(shaB) || hit(shaC) || !hit(shaD) {
		t.Fatalf("after D: B=%v C=%v D=%v", hit(shaB), hit(shaC), hit(shaD))
	}
	// Replacing an entry does not double count it.
	before := c.total
	if err := putD(); err != nil {
		t.Fatal(err)
	}
	if c.total != before {
		t.Fatalf("replace changed total %d -> %d", before, c.total)
	}
	// Eviction state survives reopen: the total is recomputed from the file.
	if err := c.Close(); err != nil {
		t.Fatal(err)
	}
	c, err = OpenSyntaxCache(dir, 2*unit+unit/2)
	if err != nil {
		t.Fatal(err)
	}
	probe = c
	clock += 2 * touchInterval
	c.now = func() int64 { return clock }
	if c.total != before {
		t.Fatalf("total after reopen %d, want %d", c.total, before)
	}
	if err := putE(); err != nil { // B is now the oldest
		t.Fatal(err)
	}
	if hit(shaB) || !hit(shaD) || !hit(shaE) {
		t.Fatalf("after reopen+E: B=%v D=%v E=%v", hit(shaB), hit(shaD), hit(shaE))
	}
}

func TestSyntaxCacheCorruptPayload(t *testing.T) {
	ctx := context.Background()
	c, err := OpenSyntaxCache(filepath.Join(t.TempDir(), "cache"), 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	sha := shaOf([]byte("x"))
	if err := c.Put(ctx, sha, "d", sampleIR("x", sha, "X")); err != nil {
		t.Fatal(err)
	}
	for _, garbage := range [][]byte{[]byte("not gzip"), mustGzip([]byte("{not json"))} {
		if err := c.Put(ctx, sha, "d", sampleIR("x", sha, "X")); err != nil {
			t.Fatal(err)
		}
		if _, err := c.writer.ExecContext(ctx, `UPDATE syntax SET payload = ? WHERE content_sha256 = ?`, garbage, sha); err != nil {
			t.Fatal(err)
		}
		if _, ok, err := c.Get(ctx, sha, "d"); err != nil || ok {
			t.Fatalf("corrupt Get = %v, %v", ok, err)
		}
		var rows int
		if err := c.writer.QueryRowContext(ctx, `SELECT COUNT(*) FROM syntax`).Scan(&rows); err != nil || rows != 0 {
			t.Fatalf("corrupt row kept: rows=%d err=%v", rows, err)
		}
		if c.total != 0 {
			t.Fatalf("total after corrupt delete = %d", c.total)
		}
	}
}

func mustGzip(b []byte) []byte {
	var buf bytes.Buffer
	w := gzip.NewWriter(&buf)
	if _, err := w.Write(b); err != nil {
		panic(err)
	}
	if err := w.Close(); err != nil {
		panic(err)
	}
	return buf.Bytes()
}

func TestSyntaxCacheConcurrent(t *testing.T) {
	ctx := context.Background()
	c, err := OpenSyntaxCache(filepath.Join(t.TempDir(), "cache"), 64<<10)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	g := new(errgroup.Group)
	for w := range 8 {
		g.Go(func() error {
			for i := range 40 {
				name := "w" + strconv.Itoa(w) + "-" + strconv.Itoa(i%10)
				sha := shaOf([]byte(name))
				if err := c.Put(ctx, sha, "d", sampleIR(name, sha, name)); err != nil {
					return err
				}
				if f, ok, err := c.Get(ctx, sha, "d"); err != nil {
					return err
				} else if ok && f.Declarations[0].Name != name {
					return errors.New("wrong payload for " + name)
				}
			}
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		t.Fatal(err)
	}
	if c.total > c.maxBytes {
		t.Fatalf("total %d over budget %d", c.total, c.maxBytes)
	}
}
