// Package serviceconfig holds the settings every codegraph process shares
// (the Spanner database, embeddings, search and logging), the Viper-based
// loader that reads them from defaults, a configuration file, the
// environment and flags, and the opener that turns them into a verified
// store and embedding provider. The worker adds its own settings on top;
// the MCP server and the operator CLIs use these alone.
package serviceconfig

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"strings"

	"ei-aitiger-codegraph/pkg/codesearch"
	spannerstore "ei-aitiger-codegraph/storage/spanner"
)

// Spanner names the database and how records are scoped and signed.
type Spanner struct {
	Database string `mapstructure:"database"`
	// Scope namespaces this deployment's data inside the database.
	Scope string `mapstructure:"scope" env:"CODEGRAPH_SCOPE"`
	// CursorSigningKey signs pagination cursors; at least 32 bytes.
	CursorSigningKey string `mapstructure:"cursor_signing_key" env:"CODEGRAPH_GRAPH_CURSOR_SIGNING_KEY"`
	// AutoProvision creates the emulator instance, database and schema at
	// startup. It requires SPANNER_EMULATOR_HOST and never touches managed
	// Spanner.
	AutoProvision bool `mapstructure:"auto_provision"`
}

// Embedding configures the embedding provider. Model and APIKey together
// enable the semantic branch of search; Dimensions fixes the vector length
// of the database's embedding column.
type Embedding struct {
	APIKey      string `mapstructure:"api_key" env:"OPENAI_API_KEY"`
	BaseURL     string `mapstructure:"base_url"`
	Model       string `mapstructure:"model"`
	Dimensions  int    `mapstructure:"dimensions"`
	Concurrency int    `mapstructure:"concurrency"`
	// MaxInputBytes bounds one embedded window; zero is the provider's
	// default (7500 bytes, inside an 8,192 token window whatever the text).
	MaxInputBytes int `mapstructure:"max_input_bytes"`
}

func (e Embedding) Enabled() bool { return e.Model != "" && e.APIKey != "" }

// VectorLength is the embedding dimension of the database: Dimensions, or
// the provider default when unset. It fixes the length of the Spanner
// embedding column and its vector index at provisioning time, so every
// process that opens the database must agree on it.
func (e Embedding) VectorLength() int {
	if e.Dimensions == 0 {
		return codesearch.DefaultDimensions
	}
	return e.Dimensions
}

// Provider builds the embedding provider, or nil when embeddings are off.
func (e Embedding) Provider() (codesearch.Provider, error) {
	if !e.Enabled() {
		return nil, nil
	}
	return codesearch.NewOpenAI(codesearch.OpenAIConfig{APIKey: e.APIKey, BaseURL: e.BaseURL, Model: e.Model, Dimensions: e.Dimensions, MaxInputBytes: e.MaxInputBytes, Logger: slog.Default()})
}

// Search tunes the agent-facing search. EnhanceQuery turns on Spanner's
// query enhancement (spelling correction, synonyms, plurals, and the IDF
// part of scoring) on managed Spanner; the emulator accepts and ignores it.
type Search struct {
	EnhanceQuery bool `mapstructure:"enhance_query"`
}

// Log configures structured logging: Level is debug, info, warn or error;
// Format is json or text.
type Log struct {
	Level  string `mapstructure:"level"`
	Format string `mapstructure:"format"`
}

// Logger builds the process logger from the settings.
func (l Log) Logger(w io.Writer) *slog.Logger {
	var level slog.Level
	_ = level.UnmarshalText([]byte(l.Level))
	opts := &slog.HandlerOptions{Level: level}
	if l.Format == "text" {
		return slog.New(slog.NewTextHandler(w, opts))
	}
	return slog.New(slog.NewJSONHandler(w, opts))
}

func (l Log) validate() error {
	var level slog.Level
	if err := level.UnmarshalText([]byte(l.Level)); err != nil {
		return errors.New("config: CODEGRAPH_LOG_LEVEL must be debug, info, warn or error")
	}
	if l.Format != "json" && l.Format != "text" {
		return errors.New("config: CODEGRAPH_LOG_FORMAT must be json or text")
	}
	return nil
}

// Config is the shared configuration. Queue names the queue of ingestion
// jobs a worker claims from (code_ingestion_jobs.queue in the Forge admin
// API's MySQL); processes that never claim jobs leave it empty.
type Config struct {
	Spanner   Spanner   `mapstructure:"spanner"`
	Embedding Embedding `mapstructure:"embedding"`
	Search    Search    `mapstructure:"search"`
	Log       Log       `mapstructure:"log"`
	Queue     string    `mapstructure:"queue"`
}

// Default is the shared configuration before any environment is read.
func Default() Config {
	return Config{
		Spanner:   Spanner{Database: "projects/codegraph-local/instances/codegraph/databases/codegraph", Scope: "codegraph"},
		Embedding: Embedding{Concurrency: codesearch.DefaultIndexConcurrency},
		Log:       Log{Level: "info", Format: "json"},
	}
}

// Validate checks the shared settings.
func (c Config) Validate() error {
	if !strings.HasPrefix(c.Spanner.Database, "projects/") || strings.Count(c.Spanner.Database, "/") != 5 {
		return errors.New("config: CODEGRAPH_SPANNER_DATABASE must be projects/<p>/instances/<i>/databases/<d>")
	}
	if len(c.Spanner.CursorSigningKey) < 32 {
		return errors.New("config: CODEGRAPH_GRAPH_CURSOR_SIGNING_KEY must be at least 32 bytes")
	}
	if c.Spanner.Scope == "" || len(c.Spanner.Scope) > 128 {
		return errors.New("config: CODEGRAPH_SCOPE required")
	}
	if c.Queue != "" && (len(c.Queue) > 128 || strings.ContainsAny(c.Queue, " :\t\r\n")) {
		return errors.New("config: queue must be a name without whitespace or colons")
	}
	if c.Embedding.Model != "" && (c.Embedding.APIKey == "" || c.Embedding.Concurrency < 1 || c.Embedding.Concurrency > 32) {
		return errors.New("config: embedding requires an API key and concurrency between 1 and 32")
	}
	if c.Embedding.MaxInputBytes != 0 && (c.Embedding.MaxInputBytes < 256 || c.Embedding.MaxInputBytes > 8000) {
		return errors.New("config: CODEGRAPH_EMBEDDING_MAX_INPUT_BYTES must be 256-8000")
	}
	if !spannerstore.ValidVectorLength(c.Embedding.VectorLength()) {
		return fmt.Errorf("config: CODEGRAPH_EMBEDDING_DIMENSIONS must be 1-%d", spannerstore.MaxVectorLength)
	}
	return c.Log.validate()
}

// LoadConfig reads the shared settings alone, then validates them.
func LoadConfig() (Config, error) {
	c := Default()
	if err := Load(&c, Options{}); err != nil {
		return Config{}, err
	}
	if err := c.Validate(); err != nil {
		return Config{}, err
	}
	return c, nil
}

// Storage is the opened store and, when configured, the embedding provider.
type Storage struct {
	Store    *spannerstore.Store
	Embedder codesearch.Provider
}

// Open provisions the emulator when asked, opens the store with the
// configured vector length, verifies the database answers with that length,
// and builds the embedding provider.
func Open(ctx context.Context, c Config) (*Storage, error) {
	if err := c.Validate(); err != nil {
		return nil, err
	}
	if c.Spanner.AutoProvision {
		if os.Getenv("SPANNER_EMULATOR_HOST") == "" {
			return nil, errors.New("config: automatic Spanner setup requires SPANNER_EMULATOR_HOST")
		}
		if err := spannerstore.ProvisionEmulator(ctx, c.Spanner.Database, c.Embedding.VectorLength()); err != nil {
			return nil, fmt.Errorf("config: provision emulator: %w", err)
		}
	}
	clientOptions, err := GoogleClientOptions()
	if err != nil {
		return nil, err
	}
	store, err := spannerstore.New(ctx, spannerstore.Config{Database: c.Spanner.Database, Scope: c.Spanner.Scope, CursorSigningKey: []byte(c.Spanner.CursorSigningKey), VectorLength: c.Embedding.VectorLength()}, clientOptions...)
	if err != nil {
		return nil, err
	}
	if err = store.Ping(ctx); err != nil {
		store.Close()
		return nil, err
	}
	embedder, err := c.Embedding.Provider()
	if err != nil {
		store.Close()
		return nil, err
	}
	return &Storage{Store: store, Embedder: embedder}, nil
}

func (s *Storage) Close() {
	if s != nil && s.Store != nil {
		s.Store.Close()
	}
}
