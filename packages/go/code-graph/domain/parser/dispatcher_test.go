package parser_test

import (
	"context"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/parser"
	"errors"
	"testing"
)

type dispatchSession struct {
	language             string
	parsed, closed, live *int
	closeError           error
}

func (s *dispatchSession) Parse(_ context.Context, in parser.Input) (ir.SourceFile, error) {
	*s.parsed++
	if in.Source.Language != s.language {
		return ir.SourceFile{}, errors.New("wrong parser selected")
	}
	return ir.SourceFile{Source: in.Source}, nil
}
func (s *dispatchSession) Close(context.Context) error { *s.closed++; *s.live--; return s.closeError }

func TestDispatcherRoutesAndOwnsOneSessionPerWorker(t *testing.T) {
	opened, closed, parsed, live := 0, 0, 0, 0
	adapter := func(name, extension string) parser.Registration {
		r := registration(name, extension)
		r.New = func() (parser.Session, error) {
			if live != 0 {
				t.Fatal("opened a parser before closing previous language")
			}
			opened++
			live++
			return &dispatchSession{name, &parsed, &closed, &live, nil}, nil
		}
		return r
	}
	registry, err := parser.NewRegistry(adapter("alpha", ".a"), adapter("beta", ".b"))
	if err != nil {
		t.Fatal(err)
	}
	dispatcher, err := parser.NewDispatcher(registry)
	if err != nil {
		t.Fatal(err)
	}
	worker := dispatcher.NewWorker()
	input := func(name, language string) parser.Input {
		return parser.Input{Source: ir.Source{Path: name, Language: language, LanguageVersion: "1"}, Limits: parser.DefaultLimits()}
	}
	for _, in := range []parser.Input{input("one.a", "alpha"), input("two.a", "alpha"), input("three.b", "beta"), input("four.a", "alpha")} {
		if _, err := worker.Parse(context.Background(), in); err != nil {
			t.Fatal(err)
		}
	}
	if opened != 3 || closed != 2 || parsed != 4 || live != 1 {
		t.Fatalf("lifecycle open=%d close=%d parse=%d live=%d", opened, closed, parsed, live)
	}
	for _, in := range []parser.Input{input("mismatch.b", "alpha"), input("unknown.zz", "alpha")} {
		if _, err := worker.Parse(context.Background(), in); !errors.Is(err, parser.ErrDispatch) {
			t.Fatal("invalid route accepted", err)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := worker.Parse(ctx, input("cancel.b", "beta")); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if opened != 3 || parsed != 4 {
		t.Fatal("rejected or canceled input reached parser")
	}
	if err := worker.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := worker.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if live != 0 || closed != 3 {
		t.Fatal("session leak or double close")
	}
	if _, err := worker.Parse(context.Background(), input("closed.a", "alpha")); !errors.Is(err, parser.ErrDispatch) {
		t.Fatal("closed worker accepted work")
	}
}

func TestDispatcherLifecycleErrorsRemainDispatchFailures(t *testing.T) {
	for _, mode := range []string{"nil-session", "factory-limit", "close-invalid"} {
		t.Run(mode, func(t *testing.T) {
			r := registration("alpha", ".a")
			count := 0
			r.New = func() (parser.Session, error) {
				if mode == "nil-session" {
					return nil, nil
				}
				if mode == "factory-limit" {
					return nil, parser.ErrLimitExceeded
				}
				return &dispatchSession{"alpha", new(int), new(int), &count, parser.ErrInvalidInput}, nil
			}
			b := registration("beta", ".b")
			registry, _ := parser.NewRegistry(r, b)
			dispatcher, _ := parser.NewDispatcher(registry)
			worker := dispatcher.NewWorker()
			defer worker.Close(context.Background())
			in := parser.Input{Source: ir.Source{Path: "one.a", Language: "alpha", LanguageVersion: "1"}, Limits: parser.DefaultLimits()}
			_, err := worker.Parse(context.Background(), in)
			if mode == "close-invalid" {
				if err != nil {
					t.Fatal(err)
				}
				in.Source.Path = "two.b"
				in.Source.Language = "beta"
				_, err = worker.Parse(context.Background(), in)
			}
			if !errors.Is(err, parser.ErrDispatch) {
				t.Fatalf("%s lost dispatch classification: %v", mode, err)
			}
		})
	}
}
