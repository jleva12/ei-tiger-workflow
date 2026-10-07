package localindex

import (
	"context"
	"math/rand"
	"runtime"
	"strconv"
	"sync"
	"testing"
	"time"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/semantic"
)

// BenchmarkSymbols100k writes 100k symbols in batches of 5000 and reads
// every one back by ID, single-threaded and then from GOMAXPROCS goroutines,
// reporting rows/second for each phase.
func BenchmarkSymbols100k(b *testing.B) {
	const n = 100_000
	ctx := context.Background()
	for range b.N {
		b.StopTimer()
		dir := b.TempDir()
		x, err := Open(ctx, dir, bc.BuildContext{ID: "bench"}, dir, nil, "d", nil, Options{})
		if err != nil {
			b.Fatal(err)
		}
		syms := make([]semantic.Symbol, n)
		ids := make([]string, n)
		for i := range syms {
			syms[i] = symbol(i, "file-"+strconv.Itoa(i%500))
			ids[i] = syms[i].ID
		}
		perm := rand.New(rand.NewSource(1)).Perm(n)
		b.StartTimer()

		start := time.Now()
		for i := 0; i < n; i += 5000 {
			if err := x.PutSymbols(ctx, syms[i:i+5000]); err != nil {
				b.Fatal(err)
			}
		}
		write := time.Since(start)

		start = time.Now()
		for _, p := range perm {
			if _, err := x.Symbol(ctx, ids[p]); err != nil {
				b.Fatal(err)
			}
		}
		read := time.Since(start)

		workers := runtime.GOMAXPROCS(0)
		var wg sync.WaitGroup
		errs := make(chan error, workers)
		start = time.Now()
		for w := range workers {
			wg.Add(1)
			go func() {
				defer wg.Done()
				for i := w; i < n; i += workers {
					if _, err := x.Symbol(ctx, ids[perm[i]]); err != nil {
						errs <- err
						return
					}
				}
			}()
		}
		wg.Wait()
		parallel := time.Since(start)
		close(errs)
		for err := range errs {
			b.Fatal(err)
		}

		b.StopTimer()
		if err := x.Close(); err != nil {
			b.Fatal(err)
		}
		b.ReportMetric(n/write.Seconds(), "write-rows/s")
		b.ReportMetric(n/read.Seconds(), "read-rows/s")
		b.ReportMetric(n/parallel.Seconds(), "parallel-read-rows/s")
		b.Logf("%d symbols: write %.0f rows/s (%v); point reads %.0f rows/s (%v); %d-way parallel point reads %.0f rows/s (%v)",
			n, n/write.Seconds(), write.Round(time.Millisecond), n/read.Seconds(), read.Round(time.Millisecond), workers, n/parallel.Seconds(), parallel.Round(time.Millisecond))
	}
}
