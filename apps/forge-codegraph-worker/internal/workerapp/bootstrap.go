package workerapp

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"sort"

	"ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/codesearch"
	"ei-aitiger-codegraph/pkg/parser"
	"ei-aitiger-codegraph/pkg/semantic"
	"ei-aitiger-codegraph/serviceconfig"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
	"ei-aitiger-codegraph/worker/internal/buildcontext/manifest"
	"ei-aitiger-codegraph/worker/internal/graphanalysis"
	"ei-aitiger-codegraph/worker/internal/ingestion"
	"ei-aitiger-codegraph/worker/internal/languages"
	"ei-aitiger-codegraph/worker/internal/localindex"
	"ei-aitiger-codegraph/worker/internal/repository/github"
)

// Dependencies are built once at process startup from the language registry.
type Dependencies struct {
	Dispatcher   *parser.Dispatcher
	BuildContext buildcontext.Provider
	// Resolvers is each enabled language's binding authority; the pipeline
	// routes every compilation context to the resolver of its source set's
	// language.
	Resolvers map[string]semantic.Resolver
	// Languages lists the resolvers' languages in lexical order.
	Languages []string
}

func Bootstrap(c Config, registry *languages.Registry) (Dependencies, error) {
	if err := c.Validate(); err != nil {
		return Dependencies{}, err
	}
	parsers, provider, err := registry.Resolve(languages.BuildConfig{Mode: c.BuildMode, Manifest: manifest.Config{ManifestPath: c.ManifestPath, Roots: c.Roots}}, c.Languages, c.ParserLimits)
	if err != nil {
		return Dependencies{}, err
	}
	dispatcher, err := parser.NewDispatcher(parsers)
	if err != nil {
		return Dependencies{}, err
	}
	resolvers, err := registry.Resolvers(c.BuildMode, c.Languages)
	if err != nil {
		return Dependencies{}, err
	}
	if len(resolvers) == 0 {
		return Dependencies{}, errors.New("worker: at least one language resolver must be configured")
	}
	deps := Dependencies{Dispatcher: dispatcher, BuildContext: provider, Resolvers: resolvers}
	for name := range resolvers {
		deps.Languages = append(deps.Languages, name)
	}
	sort.Strings(deps.Languages)
	return deps, nil
}

// ConfigurationDigest is the analysis configuration a request is bound to. It
// is computable without opening storage so admission can compute it too.
func ConfigurationDigest(c Config, deps Dependencies) string {
	return ingestion.Digest(deps.Dispatcher.Digest(), ingestion.ResolverVersions(deps.Resolvers), c.BuildLimits, c.DiscoveryLimits, c.ParserLimits)
}

// Storage owns the Spanner client shared by every command, and the
// embedding provider when one is configured.
type Storage = serviceconfig.Storage

// OpenStorage validates the worker configuration and opens the shared store.
func OpenStorage(ctx context.Context, c Config) (*Storage, error) {
	if err := c.Validate(); err != nil {
		return nil, err
	}
	return serviceconfig.Open(ctx, c.Config)
}

// Runtime is everything a worker needs to execute runs.
type Runtime struct {
	Pipeline *ingestion.Pipeline
	Cache    *localindex.SyntaxCache
	Source   *github.Service
	Embedder codesearch.Provider
}

func NewRuntime(c Config, deps Dependencies, storage *Storage, owner string, logger *slog.Logger) (_ *Runtime, err error) {
	root, err := filepath.Abs(c.WorkDir)
	if err != nil {
		return nil, err
	}
	for _, name := range []string{"mirrors", "cache", "runs"} {
		if err = os.MkdirAll(filepath.Join(root, name), 0o700); err != nil {
			return nil, err
		}
	}
	cache, err := localindex.OpenSyntaxCache(filepath.Join(root, "cache"), c.SyntaxCacheBytes)
	if err != nil {
		return nil, err
	}
	defer func() {
		if err != nil {
			cache.Close()
		}
	}()
	source, err := github.New(github.Config{
		MirrorDir: filepath.Join(root, "mirrors"), Timeout: c.CloneTimeout,
		TokenSource: staticTokenSource(c.GitHubToken),
	})
	if err != nil {
		return nil, err
	}
	embedder := storage.Embedder
	pipeline, err := ingestion.New(ingestion.Config{
		Dispatcher: deps.Dispatcher, Provider: deps.BuildContext, Resolvers: deps.Resolvers,
		Matcher: graphanalysis.Matcher{}, Projector: graphanalysis.Projector{},
		Store: storage.Store, Source: source, SyntaxCache: cache, Embedder: embedder,
		WorkDir: root, OwnerID: owner,
		ParserLimits: c.ParserLimits, BuildLimits: c.BuildLimits, DiscoveryLimits: c.DiscoveryLimits,
		Workers:          admittedWorkers(c, deps.Dispatcher),
		Loader:           spannerstore.LoaderOptions{BatchRecords: c.Loader.BatchRecords, Concurrency: c.Loader.Concurrency},
		EmbedConcurrency: c.Embedding.Concurrency, LeaseTTL: c.LeaseTTL, RenewInterval: c.RenewInterval, Logger: logger,
	})
	if err != nil {
		return nil, err
	}
	return &Runtime{Pipeline: pipeline, Cache: cache, Source: source, Embedder: embedder}, nil
}

func (r *Runtime) Close() error {
	if r == nil || r.Cache == nil {
		return nil
	}
	return r.Cache.Close()
}

// admittedWorkers bounds parser concurrency by the configured memory budget.
func admittedWorkers(c Config, d *parser.Dispatcher) int {
	reservation, err := d.WorkerReservation(c.ParserLimits)
	if err != nil || reservation == 0 {
		return 1
	}
	return max(1, int(min(uint64(c.Workers), c.WorkerBudgetBytes/reservation)))
}
