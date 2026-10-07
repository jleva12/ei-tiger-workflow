// Package languages composes language-owned configuration, parsers, build
// providers and resolvers without exposing their settings to the worker.
package languages

import (
	"encoding/json"
	"fmt"
	"sort"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/parser"
	"ei-aitiger-codegraph/pkg/semantic"
	"ei-aitiger-codegraph/worker/internal/buildcontext/composite"
	"ei-aitiger-codegraph/worker/internal/buildcontext/manifest"
	"ei-aitiger-codegraph/worker/internal/buildcontext/syntax"
)

// Settings selects enabled languages and carries each adapter's configuration.
// An empty map enables all bundled adapters with their own defaults.
type Settings map[string]json.RawMessage

type BuildConfig struct {
	Mode     string
	Manifest manifest.Config
}

// Configured is immutable. SyntaxProfile is used for explicit syntax scans.
// Discover owns language-specific build modes. Resolver is the language's
// binding authority.
type Configured struct {
	SyntaxProfile syntax.Profile
	Discover      func(mode string, inputs manifest.Config) (bc.Provider, error)
	Resolver      semantic.Resolver
}

// Adapter owns defaults, decoding and validation of its configuration namespace.
type Adapter struct {
	Parser    parser.Registration
	Configure func(json.RawMessage, string) (Configured, error)
}

type Registry struct {
	adapters map[string]Adapter
	parsers  *parser.Registry
}

func NewRegistry(adapters ...Adapter) (*Registry, error) {
	entries := make([]parser.Registration, 0, len(adapters))
	modules := make(map[string]Adapter, len(adapters))
	for _, adapter := range adapters {
		if adapter.Configure == nil {
			return nil, fmt.Errorf("%w: language configuration factory required", bc.ErrInvalidInput)
		}
		entries = append(entries, adapter.Parser)
		modules[adapter.Parser.Descriptor.Language] = adapter
	}
	parsers, err := parser.NewRegistry(entries...)
	if err != nil {
		return nil, err
	}
	for name, adapter := range modules {
		adapter.Parser, _ = parsers.Lookup(name)
		modules[name] = adapter
	}
	return &Registry{adapters: modules, parsers: parsers}, nil
}

func (r *Registry) Parsers() *parser.Registry {
	if r == nil {
		return nil
	}
	return r.parsers
}

// Resolvers returns the configured binding authority per enabled language.
func (r *Registry) Resolvers(mode string, settings Settings) (map[string]semantic.Resolver, error) {
	if r == nil {
		return nil, fmt.Errorf("%w: language registry required", bc.ErrInvalidInput)
	}
	result := make(map[string]semantic.Resolver)
	for name, adapter := range r.adapters {
		raw, selected := settings[name]
		if len(settings) > 0 && !selected {
			continue
		}
		cfg, err := adapter.Configure(append(json.RawMessage(nil), raw...), mode)
		if err != nil {
			return nil, fmt.Errorf("language %s: %w", name, err)
		}
		if cfg.Resolver == nil {
			return nil, fmt.Errorf("%w: language %s has no resolver", parser.ErrUnsupportedConfig, name)
		}
		result[name] = cfg.Resolver
	}
	for name := range settings {
		if _, ok := r.adapters[name]; !ok {
			return nil, fmt.Errorf("%w: language %s is not registered", parser.ErrUnsupportedConfig, name)
		}
	}
	return result, nil
}

// Resolve composes the parser registry and build provider for the selected
// languages and build mode. A manifest describes every language explicitly
// and syntax mode scans each language's declared roots; in the other modes
// every language's own build system describes its source sets, composed into
// one context, and a language whose build is absent falls back to its
// repository-wide syntax profile.
func (r *Registry) Resolve(build BuildConfig, settings Settings, limits parser.Limits) (*parser.Registry, bc.Provider, error) {
	if r == nil {
		return nil, nil, fmt.Errorf("%w: language registry required", bc.ErrInvalidInput)
	}
	names := make([]string, 0, len(r.adapters))
	if len(settings) == 0 {
		for name := range r.adapters {
			names = append(names, name)
		}
	} else {
		for name := range settings {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	var entries []parser.Registration
	var configured []Configured
	for _, name := range names {
		adapter, ok := r.adapters[name]
		if !ok {
			return nil, nil, fmt.Errorf("%w: language %q is not registered", parser.ErrUnsupportedConfig, name)
		}
		cfg, err := adapter.Configure(append(json.RawMessage(nil), settings[name]...), build.Mode)
		if err != nil {
			return nil, nil, fmt.Errorf("language %s: %w", name, err)
		}
		entries = append(entries, adapter.Parser)
		configured = append(configured, cfg)
	}
	selected, err := parser.NewRegistry(entries...)
	if err != nil {
		return nil, nil, err
	}
	var provider bc.Provider
	switch build.Mode {
	case "manifest":
		provider, err = manifest.New(build.Manifest)
	case "syntax":
		if len(build.Manifest.Roots) > 0 || (build.Manifest.ManifestPath != "" && build.Manifest.ManifestPath != manifest.DefaultPath) {
			return nil, nil, fmt.Errorf("%w: manifest/root settings cannot be used in syntax mode", bc.ErrInvalidInput)
		}
		profiles := make([]syntax.Profile, len(configured))
		for i, cfg := range configured {
			profile := cfg.SyntaxProfile
			if profile.Language != names[i] {
				return nil, nil, fmt.Errorf("%w: adapter returned a mismatched syntax language", bc.ErrInvalidInput)
			}
			if err := entries[i].Validate(parser.Profile{Language: profile.Language, Version: profile.Version, Options: parser.Options{EnablePreview: profile.EnablePreview, Settings: profile.Settings}}, limits); err != nil {
				return nil, nil, fmt.Errorf("language %s: %w", names[i], err)
			}
			profiles[i] = profile
		}
		provider, err = syntax.NewProfiles(profiles...)
	default:
		members := make([]composite.Member, 0, len(configured))
		for i, cfg := range configured {
			profile := cfg.SyntaxProfile
			if profile.Language != names[i] {
				return nil, nil, fmt.Errorf("%w: adapter returned a mismatched syntax language", bc.ErrInvalidInput)
			}
			if err := entries[i].Validate(parser.Profile{Language: profile.Language, Version: profile.Version, Options: parser.Options{EnablePreview: profile.EnablePreview, Settings: profile.Settings}}, limits); err != nil {
				return nil, nil, fmt.Errorf("language %s fallback profile: %w", names[i], err)
			}
			member := composite.Member{Language: names[i], Fallback: profile}
			if cfg.Discover != nil {
				if member.Provider, err = cfg.Discover(build.Mode, build.Manifest); err != nil {
					return nil, nil, fmt.Errorf("language %s: %w", names[i], err)
				}
			}
			members = append(members, member)
		}
		provider, err = composite.New(members...)
	}
	if err != nil {
		return nil, nil, err
	}
	if provider == nil {
		return nil, nil, fmt.Errorf("%w: language build provider required", bc.ErrInvalidInput)
	}
	return selected, provider, nil
}
