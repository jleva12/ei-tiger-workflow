package parser

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"path"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"
)

// Session is a worker-owned parser. Close releases its native resources.
type Session interface {
	Parser
	Close(context.Context) error
}

// Profile contains the effective syntax configuration for a source set.
type Profile struct {
	Language string
	Version  string
	Options  Options
}

// Descriptor is stable, serializable registration metadata. Change Version
// whenever implementation, configuration validation or admission policy changes.
// Extensions are exact, case-sensitive final filename extensions (including '.').
type Descriptor struct {
	Language       string
	Extensions     []string
	Version        string
	GrammarVersion string
	FeatureSet     string
}

// Registration supplies an adapter's configuration and lifecycle policy.
// Callbacks must be concurrency-safe and must not mutate their arguments.
// New returns an owned non-nil session on success and no session on error.
// WorkerMemory validates supported limits and estimates one resident session's
// bytes, including source/IR/serialization overhead; this is not a process cap.
type Registration struct {
	Descriptor   Descriptor
	New          func() (Session, error)
	Validate     func(Profile, Limits) error
	WorkerMemory func(Limits) (uint64, error)
}

// Registry is immutable after construction and safe for concurrent reads.
// It does not create parsers until a worker needs one.
type Registry struct {
	languages  map[string]Registration
	extensions map[string]string
	digest     string
}

func registryText(s string) bool {
	return s != "" && len(s) <= 4096 && strings.TrimSpace(s) == s && utf8.ValidString(s) && strings.IndexFunc(s, unicode.IsControl) < 0
}

func NewRegistry(entries ...Registration) (*Registry, error) {
	if len(entries) == 0 {
		return nil, fmt.Errorf("%w: at least one parser registration required", ErrInvalidInput)
	}
	r := &Registry{languages: map[string]Registration{}, extensions: map[string]string{}}
	var descriptors []Descriptor
	for _, entry := range entries {
		d := entry.Descriptor
		if !registryText(d.Language) || strings.ToLower(d.Language) != d.Language || !registryText(d.Version) || !registryText(d.GrammarVersion) || !registryText(d.FeatureSet) || len(d.Extensions) == 0 || entry.New == nil || entry.Validate == nil || entry.WorkerMemory == nil {
			return nil, fmt.Errorf("%w: incomplete parser registration for %q", ErrInvalidInput, d.Language)
		}
		if _, exists := r.languages[d.Language]; exists {
			return nil, fmt.Errorf("%w: duplicate parser language %q", ErrInvalidInput, d.Language)
		}
		d.Extensions = append([]string(nil), d.Extensions...)
		sort.Strings(d.Extensions)
		for _, ext := range d.Extensions {
			if !registryText(ext) || len(ext) < 2 || ext[0] != '.' || strings.ContainsAny(ext[1:], ". /\\:") {
				return nil, fmt.Errorf("%w: invalid parser extension %q", ErrInvalidInput, ext)
			}
			if owner, exists := r.extensions[ext]; exists {
				return nil, fmt.Errorf("%w: extension %q registered by both %q and %q", ErrInvalidInput, ext, owner, d.Language)
			}
			r.extensions[ext] = d.Language
		}
		entry.Descriptor = d
		r.languages[d.Language] = entry
		descriptors = append(descriptors, d)
	}
	sort.Slice(descriptors, func(i, j int) bool { return descriptors[i].Language < descriptors[j].Language })
	data, _ := json.Marshal(descriptors)
	sum := sha256.Sum256(data)
	r.digest = hex.EncodeToString(sum[:])
	return r, nil
}

func (r *Registry) Lookup(language string) (Registration, bool) {
	if r == nil {
		return Registration{}, false
	}
	entry, ok := r.languages[language]
	entry.Descriptor.Extensions = append([]string(nil), entry.Descriptor.Extensions...)
	return entry, ok
}

func (r *Registry) LanguageForPath(name string) (string, bool) {
	if r == nil {
		return "", false
	}
	language, ok := r.extensions[path.Ext(name)]
	return language, ok
}

// Digest is independent of registration/extension order and includes every
// adapter's producer metadata so a changed parser cannot reuse stale results.
func (r *Registry) Digest() string {
	if r == nil {
		return ""
	}
	return r.digest
}

// WorkerReservation returns the largest single-session estimate. Workers must
// close their current session before opening one for another language.
func (r *Registry) WorkerReservation(limits Limits) (uint64, error) {
	if r == nil || len(r.languages) == 0 {
		return 0, fmt.Errorf("%w: initialized parser registry required", ErrInvalidInput)
	}
	if err := limits.Validate(); err != nil {
		return 0, err
	}
	var languages []string
	for language := range r.languages {
		languages = append(languages, language)
	}
	sort.Strings(languages)
	var reservation uint64
	for _, language := range languages {
		n, err := r.languages[language].WorkerMemory(limits)
		if err != nil {
			return 0, fmt.Errorf("parser %s: %w", language, err)
		}
		if n == 0 {
			return 0, fmt.Errorf("%w: parser %s requires a positive memory estimate", ErrInvalidInput, language)
		}
		reservation = max(reservation, n)
	}
	return reservation, nil
}
