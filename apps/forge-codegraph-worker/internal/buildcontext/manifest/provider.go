// Package manifest implements an explicit local build inventory provider.
// It reads configuration and fingerprints inputs; it never runs repository code,
// invokes Maven/Gradle/javac, downloads dependencies, or modifies the checkout.
package manifest

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf8"

	"ei-aitiger-codegraph/pkg/buildcontext"
)

const Version = "1.1.0"
const ManifestVersion = "1.1.0"
const DefaultPath = ".codegraph/build-context.json"

// Manifest can be checked into a repository. ExpectedSnapshotID is optional:
// a checked-in file cannot embed the hash of its own future commit. The trusted
// checkout identity always pins output; a supplied expectation must match it.
type Manifest struct {
	SchemaVersion        string                 `json:"schema_version"`
	ExpectedRepositoryID string                 `json:"expected_repository_id,omitempty"`
	ExpectedSnapshotID   string                 `json:"expected_snapshot_id,omitempty"`
	Inventory            buildcontext.Inventory `json:"inventory"`
}

type Config struct {
	// ManifestPath is checkout-relative; the default is .codegraph/build-context.json.
	ManifestPath string
	// Roots maps logical names to service-owned absolute input directories.
	// The reserved root "checkout" is supplied by Request, never this map.
	Roots map[string]string
}

type Provider struct {
	path  string
	roots map[string]string
}

var _ buildcontext.Provider = (*Provider)(nil)

func New(config Config) (*Provider, error) {
	if config.ManifestPath == "" {
		config.ManifestPath = DefaultPath
	}
	if !buildcontext.ValidPath(config.ManifestPath) || config.ManifestPath == "." {
		return nil, fmt.Errorf("%w: invalid manifest path", buildcontext.ErrInvalidInput)
	}
	p := &Provider{path: config.ManifestPath, roots: map[string]string{}}
	for name, dir := range config.Roots {
		if name == "checkout" || !buildcontext.ValidRoot(name) || !filepath.IsAbs(dir) || strings.ContainsRune(dir, 0) {
			return nil, fmt.Errorf("%w: invalid configured root %q", buildcontext.ErrInvalidInput, name)
		}
		p.roots[name] = dir
	}
	return p, nil
}

func (p *Provider) Build(ctx context.Context, request buildcontext.Request) (buildcontext.BuildContext, error) {
	var zero buildcontext.BuildContext
	if err := ctx.Err(); err != nil {
		return zero, err
	}
	if err := request.Validate(); err != nil {
		return zero, err
	}
	if p == nil || p.path == "" {
		return zero, fmt.Errorf("%w: construct manifest provider with New", buildcontext.ErrInvalidInput)
	}
	root, err := os.OpenRoot(request.Checkout.Path)
	if err != nil {
		return zero, fmt.Errorf("manifest: open checkout: %w", err)
	}
	defer root.Close()
	state := &filesystem{ctx: ctx, limits: request.Limits}
	info, err := state.inspect(root, p.path)
	if err != nil {
		if errors.Is(err, errUnsupportedInput) {
			return zero, fmt.Errorf("%w: manifest path contains a symbolic link or unsupported type", buildcontext.ErrInvalidInput)
		}
		return zero, fmt.Errorf("manifest: inspect configuration: %w", err)
	}
	if !info.Mode().IsRegular() {
		return zero, fmt.Errorf("%w: manifest must be a regular file", buildcontext.ErrInvalidInput)
	}
	if uint64(info.Size()) > request.Limits.MaxInputBytes {
		return zero, fmt.Errorf("%w: manifest bytes", buildcontext.ErrLimitExceeded)
	}
	f, err := root.Open(p.path)
	if err != nil {
		return zero, fmt.Errorf("manifest: open configuration: %w", err)
	}
	data, readErr := io.ReadAll(io.LimitReader(contextReader{ctx: ctx, r: f}, int64(request.Limits.MaxInputBytes)+1))
	closeErr := f.Close()
	if err := errors.Join(readErr, closeErr); err != nil {
		return zero, err
	}
	if uint64(len(data)) > request.Limits.MaxInputBytes {
		return zero, fmt.Errorf("%w: manifest bytes", buildcontext.ErrLimitExceeded)
	}
	m, err := decode(ctx, data, request.Limits.MaxDepth)
	if err != nil {
		return zero, err
	}
	if m.SchemaVersion != ManifestVersion && m.SchemaVersion != "1.0.0" {
		return zero, fmt.Errorf("%w: manifest %q", buildcontext.ErrUnsupportedVersion, m.SchemaVersion)
	}
	if m.ExpectedRepositoryID != "" && m.ExpectedRepositoryID != request.Checkout.RepositoryID || m.ExpectedSnapshotID != "" && m.ExpectedSnapshotID != request.Checkout.SnapshotID {
		return zero, buildcontext.ErrIdentityMismatch
	}
	if m.Inventory.RecordCount() > request.Limits.MaxRecords {
		return zero, fmt.Errorf("%w: manifest records", buildcontext.ErrLimitExceeded)
	}
	if uint64(len(m.Inventory.MissingInputs)) > request.Limits.MaxDiagnostics {
		return zero, fmt.Errorf("%w: declared gaps", buildcontext.ErrLimitExceeded)
	}
	in, err := buildcontext.CanonicalInventory(m.Inventory)
	if err != nil {
		return zero, err
	}
	inputDigest, err := in.Digest()
	if err != nil {
		return zero, err
	}
	if err := ctx.Err(); err != nil {
		return zero, err
	}
	roots := map[string]*os.Root{"checkout": root}
	defer func() {
		for name, r := range roots {
			if name != "checkout" && r != nil {
				r.Close()
			}
		}
	}()
	var checks []buildcontext.InputCheck
	diagnostics := uint64(len(in.MissingInputs))
	baseRecords := in.RecordCount()
	if baseRecords+uint64(len(in.Inputs))+diagnostics > request.Limits.MaxRecords {
		return zero, fmt.Errorf("%w: context records", buildcontext.ErrLimitExceeded)
	}
	for _, input := range in.Inputs {
		if err := ctx.Err(); err != nil {
			return zero, err
		}
		check := buildcontext.InputCheck{InputID: input.ID, Status: buildcontext.Missing}
		if input.Location != nil {
			name := input.Location.Root
			r, known := roots[name]
			if !known {
				if dir, configured := p.roots[name]; configured {
					r, err = os.OpenRoot(dir)
					if err != nil && !errors.Is(err, os.ErrNotExist) {
						return zero, fmt.Errorf("manifest: open configured root %s: %w", name, err)
					}
				}
				roots[name] = r
			}
			if r != nil {
				observed, err := state.fingerprint(r, input.Location.Path, input.Kind)
				switch {
				case errors.Is(err, os.ErrNotExist):
				case errors.Is(err, errUnsupportedInput):
					check.Status = buildcontext.Unsupported
				case err != nil:
					return zero, fmt.Errorf("manifest: inspect input %s: %w", input.ID, err)
				default:
					check.Status, check.ObservedSHA256 = buildcontext.Available, observed
					if observed != input.SHA256 {
						check.Status = buildcontext.DigestMismatch
					}
				}
			}
		}
		if check.Status != buildcontext.Available {
			diagnostics++
		}
		if diagnostics > request.Limits.MaxDiagnostics {
			return zero, fmt.Errorf("%w: input diagnostics", buildcontext.ErrLimitExceeded)
		}
		if baseRecords+uint64(len(checks))+1+diagnostics > request.Limits.MaxRecords {
			return zero, fmt.Errorf("%w: context records", buildcontext.ErrLimitExceeded)
		}
		checks = append(checks, check)
	}
	result, err := buildcontext.Seal(buildcontext.BuildContext{RepositoryID: request.Checkout.RepositoryID, SnapshotID: request.Checkout.SnapshotID, Producer: buildcontext.Producer{Name: "explicit-manifest", Version: Version, InputSHA256: inputDigest}, Inventory: in, Checks: checks})
	if err != nil {
		return zero, err
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		return zero, err
	}
	if uint64(len(encoded)) > request.Limits.MaxOutputBytes {
		return zero, fmt.Errorf("%w: context output bytes", buildcontext.ErrLimitExceeded)
	}
	if err := ctx.Err(); err != nil {
		return zero, err
	}
	return result, nil
}

type contextReader struct {
	ctx context.Context
	r   io.Reader
}

func (r contextReader) Read(p []byte) (int, error) {
	if err := r.ctx.Err(); err != nil {
		return 0, err
	}
	return r.r.Read(p)
}

// Reject duplicate fields (including case variants), unknown fields, invalid
// UTF-8, oversized nesting, and trailing values rather than accepting last-wins
// configuration. Token traversal is bounded by the already checked byte limit.
func decode(ctx context.Context, data []byte, maxDepth uint32) (Manifest, error) {
	var out Manifest
	if !utf8.Valid(data) {
		return out, fmt.Errorf("%w: manifest must be UTF-8", buildcontext.ErrInvalidInput)
	}
	d := json.NewDecoder(bytes.NewReader(data))
	d.UseNumber()
	var value func(uint32) error
	value = func(depth uint32) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		if depth > maxDepth {
			return fmt.Errorf("%w: JSON nesting", buildcontext.ErrLimitExceeded)
		}
		token, err := d.Token()
		if err != nil {
			return err
		}
		switch token {
		case json.Delim('{'):
			seen := map[string]bool{}
			for d.More() {
				key, err := d.Token()
				if err != nil {
					return err
				}
				name, ok := key.(string)
				if !ok || name != strings.ToLower(name) || seen[name] {
					return fmt.Errorf("duplicate or noncanonical JSON field %q", name)
				}
				seen[name] = true
				if err := value(depth + 1); err != nil {
					return err
				}
			}
			_, err = d.Token()
			return err
		case json.Delim('['):
			for d.More() {
				if err := value(depth + 1); err != nil {
					return err
				}
			}
			_, err = d.Token()
			return err
		default:
			return nil
		}
	}
	if err := value(1); err != nil {
		if errors.Is(err, buildcontext.ErrLimitExceeded) || ctx.Err() != nil {
			return out, err
		}
		return out, fmt.Errorf("%w: %w", buildcontext.ErrInvalidInput, err)
	}
	if _, err := d.Token(); err != io.EOF {
		return out, fmt.Errorf("%w: trailing manifest content", buildcontext.ErrInvalidInput)
	}
	d = json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	if err := d.Decode(&out); err != nil {
		return Manifest{}, fmt.Errorf("%w: %w", buildcontext.ErrInvalidInput, err)
	}
	return out, nil
}
