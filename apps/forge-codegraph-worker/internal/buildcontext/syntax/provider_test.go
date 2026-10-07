package syntax

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"sync"
	"testing"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

func TestExplicitProfilePreservesUnknownBuildInputs(t *testing.T) {
	r := bc.Request{Checkout: bc.Checkout{Path: t.TempDir(), RepositoryID: "repo", SnapshotID: "commit"}, Limits: bc.DefaultLimits()}
	ids := map[bc.ID]bool{}
	for _, release := range []int{8, 11, 17, 21} {
		p, err := New(release)
		if err != nil {
			t.Fatal(err)
		}
		c, err := p.Build(context.Background(), r)
		if err != nil {
			t.Fatal(err)
		}
		if err := c.Validate(); err != nil {
			t.Fatal(err)
		}
		if c.Status != bc.Incomplete || len(c.Diagnostics) != 2 || len(c.Inventory.MissingInputs) != 1 || c.Inventory.JDKs[0].Vendor != "unknown" || c.Inventory.SourceSets[0].TargetRelease != release || len(c.Inventory.SourceSets[0].Classpath) != 0 {
			t.Fatalf("invented build knowledge: %+v", c)
		}
		if ids[c.ID] {
			t.Fatal("release omitted from identity")
		}
		ids[c.ID] = true
		// A different absolute mount preserves the content identity.
		other := r
		other.Checkout.Path = t.TempDir()
		again, err := p.Build(context.Background(), other)
		if err != nil || !reflect.DeepEqual(c, again) {
			t.Fatal("mount affects identity", err)
		}
	}
	for _, release := range []int{0, 7, 22} {
		if _, err := New(release); err == nil {
			t.Fatal("accepted profile", release)
		}
	}
}

func TestProfileLimitsCancellationAndOwnership(t *testing.T) {
	p, _ := New(21)
	r := bc.Request{Checkout: bc.Checkout{Path: t.TempDir(), RepositoryID: "repo", SnapshotID: "commit"}, Limits: bc.DefaultLimits()}
	c, err := p.Build(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	data, _ := json.Marshal(c)
	for _, mutate := range []func(*bc.Limits){func(l *bc.Limits) { l.MaxOutputBytes = uint64(len(data) - 1) }, func(l *bc.Limits) { l.MaxRecords = c.RecordCount() - 1 }, func(l *bc.Limits) { l.MaxDiagnostics = 1 }} {
		bad := r
		mutate(&bad.Limits)
		if _, err := p.Build(context.Background(), bad); !errors.Is(err, bc.ErrLimitExceeded) {
			t.Fatal("limit", err)
		}
	}
	exact := r
	exact.Limits.MaxOutputBytes = uint64(len(data))
	if _, err := p.Build(context.Background(), exact); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := p.Build(ctx, r); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for range 4 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			out, err := p.Build(context.Background(), r)
			if err != nil {
				t.Error(err)
				return
			}
			out.Inventory.SourceSets[0].Name = "mutated"
		}()
	}
	wg.Wait()
	again, err := p.Build(context.Background(), r)
	if err != nil || !reflect.DeepEqual(c, again) {
		t.Fatal("ownership", err)
	}
}
