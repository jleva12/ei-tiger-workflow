package ingestion

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"strings"
	"testing"
	"time"
)

func TestProgressThrottlesAndCountsLeft(t *testing.T) {
	var out bytes.Buffer
	log := slog.New(slog.NewJSONHandler(&out, nil))
	p := newProgress(log, "parse", "files", 10)
	p.add(3)
	if out.Len() != 0 {
		t.Fatalf("a progress line before the interval: %s", out.String())
	}
	p.next = time.Now().Add(-time.Second) // the interval has passed
	p.add(1)
	p.add(1) // throttled again
	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	if len(lines) != 1 {
		t.Fatalf("want one progress line, got %d: %s", len(lines), out.String())
	}
	var line map[string]any
	if err := json.Unmarshal([]byte(lines[0]), &line); err != nil {
		t.Fatal(err)
	}
	for k, want := range map[string]any{"msg": "phase progress", "phase": "parse", "step": "5/11", "unit": "files", "done": 4.0, "total": 10.0, "left": 6.0, "percent": 40.0} {
		if line[k] != want {
			t.Errorf("%s = %v, want %v", k, line[k], want)
		}
	}
	if _, ok := line["eta"]; !ok {
		t.Error("no eta")
	}
}

func TestProgressWithoutTotalCountsUp(t *testing.T) {
	var out bytes.Buffer
	p := newProgress(slog.New(slog.NewJSONHandler(&out, nil)), "embed", "documents", 0)
	p.next = time.Time{}
	p.set(256)
	if !strings.Contains(out.String(), `"done":256`) || strings.Contains(out.String(), `"left"`) {
		t.Fatalf("progress without a total: %s", out.String())
	}
}

func TestPhaseLinesNameTheirStep(t *testing.T) {
	var out bytes.Buffer
	log := slog.New(slog.NewJSONHandler(&out, nil))
	clock := newClock()
	clock.begin(log, "resolve", "files", 3)
	clock.finish(log, "resolve")
	if clock.current != "resolve" || !strings.Contains(out.String(), `"msg":"phase started","phase":"resolve","step":"6/11","files":3`) || !strings.Contains(out.String(), `"msg":"phase finished","phase":"resolve","step":"6/11","elapsed"`) {
		t.Fatalf("phase lines: %s", out.String())
	}
	if _, ok := clock.durations["resolve"]; !ok {
		t.Fatal("the phase's duration was not recorded")
	}
}
