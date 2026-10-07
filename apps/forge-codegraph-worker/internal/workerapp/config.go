// Package workerapp assembles the worker from configuration: language
// adapters, the Spanner store, the git mirror, the syntax cache and the
// ingestion pipeline, and runs the job loop with health probes.
package workerapp

import (
	"errors"
	"net"
	"strings"
	"time"
	"unicode"

	"ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/ingesttask"
	"ei-aitiger-codegraph/pkg/parser"
	"ei-aitiger-codegraph/serviceconfig"
	"ei-aitiger-codegraph/worker/internal/buildcontext/manifest"
	"ei-aitiger-codegraph/worker/internal/discovery"
	"ei-aitiger-codegraph/worker/internal/languages"
)

// The Spanner, embedding and search settings are the shared service
// configuration; the worker adds its own on top.
type (
	SpannerConfig   = serviceconfig.Spanner
	EmbeddingConfig = serviceconfig.Embedding
	SearchConfig    = serviceconfig.Search
)

type LoaderConfig struct {
	Concurrency  int
	BatchRecords int
}

// Config is local worker configuration. Task payloads cannot change
// credentials, paths, parsers or budgets. The embedded shared configuration
// contributes Spanner, Embedding, Search and Queue.
type Config struct {
	serviceconfig.Config `mapstructure:",squash"`
	TaskConcurrency      int           `mapstructure:"task_concurrency"`
	ShutdownTimeout      time.Duration `mapstructure:"shutdown_timeout"`
	StartupTimeout       time.Duration `mapstructure:"startup_timeout"`
	CleanupTimeout       time.Duration `mapstructure:"cleanup_timeout"`
	HealthAddr           string        `mapstructure:"health_addr"`
	// AdmissionToken turns on the worker's API (graph reads, runs, the audit
	// and cross-repository links) on the health listener; callers present it
	// as a bearer token. Empty leaves the API off. Named for when the API
	// also admitted ingestions, which the Forge admin API now queues.
	AdmissionToken string `mapstructure:"admission_token"`
	// JobsMySQLDSN is the Forge admin API's MySQL, whose code_ingestion_jobs
	// table is the queue the worker claims ingestions from: a go-sql-driver
	// DSN, user:password@tcp(host:port)/database (internal/jobqueue).
	JobsMySQLDSN string `mapstructure:"jobs_mysql_dsn"`
	// GitHubToken authenticates every clone, fetch and ls-remote of a GitHub
	// repository, e.g. a fine-grained personal access token that reads the
	// repositories' contents. Empty fetches anonymously: public repositories
	// only.
	GitHubToken       string              `mapstructure:"github_token"`
	WorkDir           string              `mapstructure:"work_dir"`
	BuildMode         string              `mapstructure:"build_mode"`
	ManifestPath      string              `mapstructure:"manifest"`
	Languages         languages.Settings  `mapstructure:"languages"`
	Roots             map[string]string   `mapstructure:"roots"`
	Timeout           time.Duration       `mapstructure:"timeout"`
	CloneTimeout      time.Duration       `mapstructure:"clone_timeout"`
	Workers           int                 `mapstructure:"workers"`
	WorkerBudgetBytes uint64              `mapstructure:"worker_budget_bytes"`
	ParserLimits      parser.Limits       `mapstructure:"parser"`
	BuildLimits       buildcontext.Limits `mapstructure:"build"`
	DiscoveryLimits   discovery.Limits    `mapstructure:"discovery"`
	SyntaxCacheBytes  int64               `mapstructure:"syntax_cache_bytes"`
	LeaseTTL          time.Duration       `mapstructure:"lease_ttl"`
	RenewInterval     time.Duration       `mapstructure:"lease_renew_interval"`
	MaxAttempts       int                 `mapstructure:"max_attempts"`
	Loader            LoaderConfig        `mapstructure:"loader"`
}

func DefaultConfig() Config {
	shared := serviceconfig.Default()
	shared.Queue = ingesttask.Queue
	return Config{
		Config:            shared,
		TaskConcurrency:   1,
		ShutdownTimeout:   30 * time.Second,
		StartupTimeout:    15 * time.Second,
		CleanupTimeout:    15 * time.Second,
		HealthAddr:        "127.0.0.1:8090",
		WorkDir:           ".codegraph-work",
		BuildMode:         "auto",
		ManifestPath:      manifest.DefaultPath,
		Languages:         languages.Settings{},
		Roots:             map[string]string{},
		Timeout:           2 * time.Hour,
		CloneTimeout:      10 * time.Minute,
		Workers:           8,
		WorkerBudgetBytes: 2 << 30,
		ParserLimits:      parser.DefaultLimits(),
		BuildLimits:       buildcontext.DefaultLimits(),
		DiscoveryLimits:   discovery.DefaultLimits(),
		SyntaxCacheBytes:  2 << 30,
		LeaseTTL:          2 * time.Minute,
		RenewInterval:     30 * time.Second,
		MaxAttempts:       6,
		Loader:            LoaderConfig{Concurrency: 8, BatchRecords: 1000},
	}
}

func (c Config) Validate() error {
	if err := c.Config.Validate(); err != nil {
		return err
	}
	if c.Queue == "" {
		return errors.New("worker: queue must be a nonempty name")
	}
	for name, dir := range c.Roots {
		if !buildcontext.ValidRoot(name) || name == "checkout" || !strings.HasPrefix(dir, "/") {
			return errors.New("invalid CODEGRAPH_ROOTS; names must identify logical roots with absolute directories; checkout is reserved")
		}
	}
	if c.Timeout <= 0 || c.CloneTimeout <= 0 || c.ShutdownTimeout <= 0 || c.StartupTimeout <= 0 || c.CleanupTimeout <= 0 {
		return errors.New("worker: timeouts must be positive")
	}
	if c.LeaseTTL < 30*time.Second || c.RenewInterval <= 0 || c.RenewInterval >= c.LeaseTTL/2 {
		return errors.New("worker: lease TTL must be at least 30s and renew interval less than half of it")
	}
	if c.HealthAddr != "" {
		if _, _, err := net.SplitHostPort(c.HealthAddr); err != nil {
			return errors.New("worker: CODEGRAPH_HEALTH_ADDR must be host:port or empty to disable")
		}
	}
	if strings.IndexFunc(c.GitHubToken, func(r rune) bool { return unicode.IsSpace(r) || unicode.IsControl(r) }) >= 0 {
		return errors.New("worker: CODEGRAPH_GITHUB_TOKEN must not contain spaces or control characters")
	}
	if strings.TrimSpace(c.JobsMySQLDSN) == "" {
		return errors.New("worker: CODEGRAPH_JOBS_MYSQL_DSN is required: the Forge admin API's MySQL, where ingestions are queued")
	}
	if c.AdmissionToken != "" {
		if len(c.AdmissionToken) < 32 {
			return errors.New("worker: CODEGRAPH_ADMISSION_TOKEN must be at least 32 characters")
		}
		if c.HealthAddr == "" {
			return errors.New("worker: the admission API needs CODEGRAPH_HEALTH_ADDR")
		}
	}
	if c.TaskConcurrency < 1 || c.TaskConcurrency > 16 {
		return errors.New("worker: task concurrency must be between 1 and 16")
	}
	if c.Workers < 1 || c.Workers > 64 || c.WorkerBudgetBytes == 0 {
		return errors.New("worker: parser workers must be between 1 and 64 with a positive memory budget")
	}
	if strings.TrimSpace(c.WorkDir) == "" {
		return errors.New("worker: work directory required")
	}
	if err := c.ParserLimits.Validate(); err != nil {
		return err
	}
	if err := c.BuildLimits.Validate(); err != nil {
		return err
	}
	if err := c.DiscoveryLimits.Validate(); err != nil {
		return err
	}
	if c.Loader.Concurrency < 1 || c.Loader.Concurrency > 64 || c.Loader.BatchRecords < 1 || c.Loader.BatchRecords > 5000 {
		return errors.New("worker: loader concurrency 1-64 and batch records 1-5000 required")
	}
	if c.SyntaxCacheBytes < 64<<20 {
		return errors.New("worker: syntax cache must be at least 64 MiB")
	}
	if c.MaxAttempts < 1 || c.MaxAttempts > 100 {
		return errors.New("worker: max attempts must be between 1 and 100")
	}
	return nil
}
