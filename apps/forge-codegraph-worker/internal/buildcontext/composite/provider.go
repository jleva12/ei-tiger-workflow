// Package composite describes a checkout that holds several languages by
// composing one build provider per language into a single build context. A
// language whose build system declares source sets contributes them as is;
// a language with no build provider, or whose provider reports that the
// checkout has no build for it, is covered by a repository-wide syntax
// profile, so every registered file is discovered and the pipeline decides
// per compilation context which resolver binds it. Source sets declared by
// a build always take precedence: the fallback is created only when a
// language has none.
package composite

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/worker/internal/buildcontext/syntax"
)

// Version changes when the composition rules change; it is part of the
// producer identity of every composed context. 1.1.0: builds that scan by
// syntax get a module of their own when more than one does.
const Version = "1.1.0"

// Member is one language's contribution.
type Member struct {
	Language string
	// Provider is the language's build system; nil when the language has none.
	Provider bc.Provider
	// Fallback is the repository-wide syntax profile used when Provider is
	// nil, reports bc.ErrNoBuild, or declares no source sets.
	Fallback syntax.Profile
}

type Provider struct {
	members []Member
}

var _ bc.Provider = (*Provider)(nil)

// New composes the members, one per language, in lexical language order.
func New(members ...Member) (*Provider, error) {
	if len(members) == 0 {
		return nil, fmt.Errorf("%w: at least one language required", bc.ErrInvalidInput)
	}
	out := append([]Member(nil), members...)
	sort.Slice(out, func(i, j int) bool { return out[i].Language < out[j].Language })
	for i, m := range out {
		if m.Language == "" || m.Fallback.Language != m.Language || m.Fallback.Version == "" || len(m.Fallback.Roots) == 0 {
			return nil, fmt.Errorf("%w: language %q needs a fallback syntax profile for itself", bc.ErrInvalidInput, m.Language)
		}
		if i > 0 && out[i-1].Language == m.Language {
			return nil, fmt.Errorf("%w: language %s composed twice", bc.ErrInvalidInput, m.Language)
		}
		// The fallback provider is validated once, not on every build.
		if _, err := syntax.NewProfiles(m.Fallback); err != nil {
			return nil, fmt.Errorf("language %s fallback: %w", m.Language, err)
		}
	}
	return &Provider{members: out}, nil
}

// Languages lists the composed languages in order.
func (p *Provider) Languages() []string {
	out := make([]string, len(p.members))
	for i, m := range p.members {
		out[i] = m.Language
	}
	return out
}

// contribution is one member's built context and how it was obtained.
type contribution struct {
	language string
	context  bc.BuildContext
	fallback bool
}

// Build runs every member, merges the inventories and seals one context.
// Identifiers must be unique across members; a build system that reuses
// another's identifiers is reported, never silently renamed.
func (p *Provider) Build(ctx context.Context, r bc.Request) (bc.BuildContext, error) {
	if err := ctx.Err(); err != nil {
		return bc.BuildContext{}, err
	}
	if err := r.Validate(); err != nil {
		return bc.BuildContext{}, err
	}
	contributions := make([]contribution, 0, len(p.members))
	for _, m := range p.members {
		c, err := p.build(ctx, r, m)
		if err != nil {
			return bc.BuildContext{}, err
		}
		contributions = append(contributions, c)
	}
	separateSyntaxScans(contributions)
	merged, checks, producers, err := merge(contributions)
	if err != nil {
		return bc.BuildContext{}, err
	}
	digest, err := merged.Digest()
	if err != nil {
		return bc.BuildContext{}, err
	}
	sealed, err := bc.Seal(bc.BuildContext{RepositoryID: r.Checkout.RepositoryID, SnapshotID: r.Checkout.SnapshotID,
		Producer: bc.Producer{Name: "composite-languages", Version: Version, InputSHA256: producerDigest(producers, digest)}, Inventory: merged, Checks: checks})
	if err != nil {
		return bc.BuildContext{}, err
	}
	data, err := json.Marshal(sealed)
	if err != nil {
		return bc.BuildContext{}, err
	}
	if sealed.RecordCount() > r.Limits.MaxRecords || uint64(len(sealed.Diagnostics)) > r.Limits.MaxDiagnostics || uint64(len(data)) > r.Limits.MaxOutputBytes {
		return bc.BuildContext{}, bc.ErrLimitExceeded
	}
	return sealed, nil
}

// build obtains one member's context: its build system when it declares
// source sets, the fallback profile otherwise. A build system that fails
// (a dependency it cannot download, a plugin that breaks, a project layout
// it cannot read) leaves its language to the fallback profile with a gap
// saying why; one language never fails the others.
func (p *Provider) build(ctx context.Context, r bc.Request, m Member) (contribution, error) {
	var failure error
	if m.Provider != nil {
		c, err := m.Provider.Build(ctx, r)
		switch {
		case errors.Is(err, bc.ErrNoBuild):
		case err != nil && ctx.Err() != nil:
			return contribution{}, fmt.Errorf("language %s: %w", m.Language, err)
		case err != nil:
			failure = err
			slog.WarnContext(ctx, "the build of a language failed; its sources are analysed without it", "language", m.Language, "error", err)
		case len(c.Inventory.SourceSets) > 0:
			return contribution{language: m.Language, context: c}, nil
		}
	}
	fallback, err := syntax.NewProfiles(m.Fallback)
	if err != nil {
		return contribution{}, fmt.Errorf("language %s fallback: %w", m.Language, err)
	}
	c, err := fallback.Build(ctx, r)
	if err != nil {
		return contribution{}, fmt.Errorf("language %s fallback: %w", m.Language, err)
	}
	c = namespaceSyntax(c, m.Language)
	if failure != nil {
		c.Inventory.MissingInputs = append(c.Inventory.MissingInputs, bc.MissingInput{ID: bc.GapID("build-failed-" + m.Language), Requested: bc.GapBuildFailed, Reason: gapReason(fmt.Sprintf("The %s build failed: %v", m.Language, failure))})
	}
	return contribution{language: m.Language, context: c, fallback: true}, nil
}

// gapReason is text a gap can carry: one line, no control characters, at
// most 2 KB, cut on a character boundary.
func gapReason(text string) string {
	text = strings.Map(func(r rune) rune {
		if unicode.IsControl(r) {
			return ' '
		}
		return r
	}, strings.ToValidUTF8(text, "?"))
	text = strings.Join(strings.Fields(text), " ")
	if len(text) > 2048 {
		cut := 2045
		for cut > 0 && !utf8.RuneStart(text[cut]) {
			cut--
		}
		text = text[:cut] + "..."
	}
	return text
}

// separateSyntaxScans gives each language's syntax scan its own module when
// more than one build brings one. Build providers that scan by syntax (the
// Python and TypeScript projects) name their module "syntax", as the syntax
// provider does, so a checkout with both would otherwise declare it twice.
// A lone one keeps its name: a file's identity includes its module, and
// renaming it would give every file already in the graph a new identity.
func separateSyntaxScans(contributions []contribution) {
	var scans []int
	for i, c := range contributions {
		for _, m := range c.context.Inventory.Modules {
			if m.ID == "syntax" {
				scans = append(scans, i)
				break
			}
		}
	}
	if len(scans) < 2 {
		return
	}
	for _, i := range scans {
		contributions[i].context = namespaceSyntax(contributions[i].context, contributions[i].language)
	}
}

// namespaceSyntax renames the syntax provider's shared module and gap so
// two languages' syntax scans never collide. Fallbacks always get it.
func namespaceSyntax(c bc.BuildContext, language string) bc.BuildContext {
	module := bc.ModuleID("syntax-" + language)
	for i := range c.Inventory.Modules {
		if c.Inventory.Modules[i].ID == "syntax" {
			c.Inventory.Modules[i].ID = module
			c.Inventory.Modules[i].Name = language + " syntax scan"
		}
	}
	for i := range c.Inventory.SourceSets {
		if c.Inventory.SourceSets[i].ModuleID == "syntax" {
			c.Inventory.SourceSets[i].ModuleID = module
		}
	}
	for i := range c.Inventory.MissingInputs {
		if c.Inventory.MissingInputs[i].ID == "build-inventory" {
			c.Inventory.MissingInputs[i].ID = bc.GapID("syntax-" + language + "-build-inventory")
			c.Inventory.MissingInputs[i].Requested = language + " " + c.Inventory.MissingInputs[i].Requested
		}
		if c.Inventory.MissingInputs[i].ModuleID == "syntax" {
			c.Inventory.MissingInputs[i].ModuleID = module
		}
	}
	return c
}

type producer struct {
	Language string      `json:"language"`
	Producer bc.Producer `json:"producer"`
	Fallback bool        `json:"fallback"`
}

// merge concatenates the members' inventories and input checks, rejecting
// any identifier declared by more than one member.
func merge(contributions []contribution) (bc.Inventory, []bc.InputCheck, []producer, error) {
	var out bc.Inventory
	var checks []bc.InputCheck
	var producers []producer
	owners := map[string]string{}
	claim := func(kind, id, language string) error {
		key := kind + ":" + id
		if previous, taken := owners[key]; taken {
			return fmt.Errorf("%w: %s %s is declared by both %s and %s", bc.ErrInvalidInput, kind, id, previous, language)
		}
		owners[key] = language
		return nil
	}
	for _, c := range contributions {
		in := c.context.Inventory
		for _, x := range in.Inputs {
			if err := claim("input", string(x.ID), c.language); err != nil {
				return bc.Inventory{}, nil, nil, err
			}
		}
		for _, x := range in.JDKs {
			if err := claim("jdk", string(x.ID), c.language); err != nil {
				return bc.Inventory{}, nil, nil, err
			}
		}
		for _, x := range in.Modules {
			if err := claim("module", string(x.ID), c.language); err != nil {
				return bc.Inventory{}, nil, nil, err
			}
		}
		for _, x := range in.SourceSets {
			if err := claim("source_set", string(x.ID), c.language); err != nil {
				return bc.Inventory{}, nil, nil, err
			}
		}
		for _, x := range in.Artifacts {
			if err := claim("artifact", string(x.ID), c.language); err != nil {
				return bc.Inventory{}, nil, nil, err
			}
		}
		for _, x := range in.MissingInputs {
			if err := claim("missing_input", string(x.ID), c.language); err != nil {
				return bc.Inventory{}, nil, nil, err
			}
		}
		out.Inputs = append(out.Inputs, in.Inputs...)
		out.JDKs = append(out.JDKs, in.JDKs...)
		out.Modules = append(out.Modules, in.Modules...)
		out.SourceSets = append(out.SourceSets, in.SourceSets...)
		out.Artifacts = append(out.Artifacts, in.Artifacts...)
		out.MissingInputs = append(out.MissingInputs, in.MissingInputs...)
		checks = append(checks, c.context.Checks...)
		producers = append(producers, producer{Language: c.language, Producer: c.context.Producer, Fallback: c.fallback})
	}
	return out, checks, producers, nil
}

// producerDigest fingerprints the composition: which producer described each
// language, whether it was the fallback, and the merged inventory.
func producerDigest(producers []producer, inventory string) string {
	return digestOf(struct {
		Version   string     `json:"version"`
		Producers []producer `json:"producers"`
		Inventory string     `json:"inventory"`
	}{Version, producers, inventory})
}
