package ingestion

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"strings"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/semantic"
)

// ResolverVersion fingerprints one language's binding authority.
type ResolverVersion struct {
	Language string `json:"language"`
	Version  string `json:"version"`
	Policy   string `json:"policy"`
}

// ResolverVersions lists the resolvers' identities in lexical language
// order, the form the analysis configuration digest hashes.
func ResolverVersions(resolvers map[string]semantic.Resolver) []ResolverVersion {
	out := make([]ResolverVersion, 0, len(resolvers))
	for language, r := range resolvers {
		out = append(out, ResolverVersion{Language: language, Version: r.Version(), Policy: r.PolicyDigest()})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Language < out[j].Language })
	return out
}

// routeContexts groups the affected compilation contexts by the language of
// their source set, so each language's resolver attributes only its own
// contexts. A context outside the inventory, or a language without a
// resolver, is a configuration error, never a silent skip.
func routeContexts(contexts []bc.SourceSetID, sets map[string]bc.SourceSet, resolvers map[string]semantic.Resolver) (map[string][]bc.SourceSetID, error) {
	out := map[string][]bc.SourceSetID{}
	for _, id := range contexts {
		set, ok := sets[string(id)]
		if !ok {
			return nil, fmt.Errorf("context %s is not in the build inventory", id)
		}
		language, _ := set.SyntaxLanguage()
		if _, ok := resolvers[language]; !ok {
			return nil, fmt.Errorf("no resolver for language %s of context %s", language, id)
		}
		out[language] = append(out[language], id)
	}
	for _, ids := range out {
		sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	}
	return out, nil
}

// resolve attributes the affected contexts language by language, in lexical
// order, and sums the counts.
func (p *Pipeline) resolve(ctx context.Context, log *slog.Logger, key deployment.RunKey, commit, checkoutPath string, contexts []bc.SourceSetID, sets map[string]bc.SourceSet, index semantic.Workspace) (semantic.ResolutionResult, error) {
	routed, err := routeContexts(contexts, sets, p.config.Resolvers)
	if err != nil {
		return semantic.ResolutionResult{}, err
	}
	languages := make([]string, 0, len(routed))
	for language := range routed {
		languages = append(languages, language)
	}
	sort.Strings(languages)
	var total semantic.ResolutionResult
	for _, language := range languages {
		if log != nil {
			log.Info("language resolving", "language", language, "contexts", len(routed[language]))
		}
		r, err := p.config.Resolvers[language].Resolve(ctx, semantic.ResolveRequest{Run: key, CommitSHA: commit, CheckoutPath: checkoutPath, Contexts: routed[language], SyntaxLimits: p.config.ParserLimits, BuildLimits: p.config.BuildLimits}, index)
		if err != nil {
			if ctx.Err() != nil || errors.Is(err, semantic.ErrIntegrity) {
				return total, fmt.Errorf("%s: %w", language, err)
			}
			// One language's resolver failing leaves its files with their
			// declarations and whatever bindings it stored; the other
			// languages still resolve, and the next run tries it again.
			if log != nil {
				log.Warn("a language's resolver failed; its references are unresolved in this generation", "language", language, "contexts", len(routed[language]), "error", err)
			}
			total.Warnings = append(total.Warnings, fmt.Sprintf("The %s resolver failed (%s), so its references are unresolved in this commit.", language, strings.Join(strings.Fields(err.Error()), " ")))
			for _, id := range routed[language] {
				total.Degraded = append(total.Degraded, string(id))
			}
			continue
		}
		if log != nil {
			log.Info("language resolved", "language", language, "contexts", len(routed[language]), "symbols", r.Symbols, "resolved", r.Resolved, "unresolved", r.Unresolved, "ambiguous", r.Ambiguous, "unsupported", r.Unsupported, "skipped", r.Skipped)
		}
		total.Symbols += r.Symbols
		total.Resolved += r.Resolved
		total.Unresolved += r.Unresolved
		total.Ambiguous += r.Ambiguous
		total.Unsupported += r.Unsupported
		total.Skipped += r.Skipped
		total.Warnings = append(total.Warnings, r.Warnings...)
		total.CompiledOutputs = append(total.CompiledOutputs, r.CompiledOutputs...)
		total.Degraded = append(total.Degraded, r.Degraded...)
	}
	return total, nil
}
