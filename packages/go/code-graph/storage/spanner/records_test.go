package spannerstore

import (
	"errors"
	"strings"
	"testing"

	"cloud.google.com/go/spanner"

	"ei-aitiger-codegraph/pkg/graph"
)

func TestOpenAt(t *testing.T) {
	open := spanner.NullInt64{}
	closedAt5 := spanner.NullInt64{Int64: 5, Valid: true}
	cases := []struct {
		from int64
		to   spanner.NullInt64
		gen  uint64
		want bool
	}{
		{1, open, 1, true}, {1, open, 9, true}, {2, open, 1, false},
		{1, closedAt5, 4, true}, {1, closedAt5, 5, false}, {1, closedAt5, 6, false},
		{3, closedAt5, 3, true}, {3, closedAt5, 2, false},
		{1, open, 0, false},
	}
	for _, c := range cases {
		if got := openAt(c.from, c.to, c.gen); got != c.want {
			t.Errorf("openAt(%d,%v,%d)=%v want %v", c.from, c.to, c.gen, got, c.want)
		}
	}
}

func TestOpenPredicate(t *testing.T) {
	if got := openPredicate(""); got != "GenFrom<=@gen AND (GenTo IS NULL OR GenTo>@gen)" {
		t.Fatalf("bare predicate: %s", got)
	}
	if got := openPredicate("r"); !strings.HasPrefix(got, "r.GenFrom<=@gen AND (r.GenTo IS NULL OR r.GenTo>@gen)") {
		t.Fatalf("aliased predicate: %s", got)
	}
}

func TestResolveGeneration(t *testing.T) {
	if g, err := resolveGeneration(4, 0); err != nil || g != 4 {
		t.Fatalf("zero means live: %d %v", g, err)
	}
	if g, err := resolveGeneration(4, 2); err != nil || g != 2 {
		t.Fatalf("explicit: %d %v", g, err)
	}
	if _, err := resolveGeneration(4, 5); !errors.Is(err, graph.ErrInvalid) {
		t.Fatalf("above live must be rejected: %v", err)
	}
	if g, err := resolveGeneration(0, 0); err != nil || g != 0 {
		t.Fatalf("empty repository: %d %v", g, err)
	}
}
