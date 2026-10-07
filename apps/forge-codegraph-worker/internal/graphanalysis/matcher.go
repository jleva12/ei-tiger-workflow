package graphanalysis

import (
	"context"
	"errors"
	"fmt"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

// Matcher assigns persistent entity and occurrence IDs per file lineage.
//
// A declaration continues its previous entity when (a) the file's content is
// unchanged and the same local declaration ID still names the same name, span
// and owner, or (b) its canonical declaration key existed exactly once in the
// previous map and exists exactly once now. Everything else, including renames
// and ambiguous correspondences, allocates a fresh run-scoped identity.
// Occurrences continue only when the content is unchanged and the site is
// still enclosed by the same entity.
type Matcher struct{}

var _ semantic.Matcher = Matcher{}

func (Matcher) Match(ctx context.Context, in semantic.MatchRequest, w semantic.Workspace) (out semantic.MatchResult, err error) {
	if w == nil || in.Run.Validate() != nil {
		return out, fmt.Errorf("%w: run key and workspace required", semantic.ErrInvalid)
	}
	for _, f := range in.Files {
		if err := ctx.Err(); err != nil {
			return out, err
		}
		if !graph.ValidID(f.Lineage) {
			return out, fmt.Errorf("%w: file %s has no lineage", semantic.ErrInvalid, f.Source.Path)
		}
		r, err := matchFile(ctx, in.Run, f, w)
		if err != nil {
			return out, fmt.Errorf("match %s: %w", f.Source.Path, err)
		}
		out.Allocated += r.Allocated
		out.Continued += r.Continued
		out.Ambiguous += r.Ambiguous
	}
	return out, nil
}

func matchFile(ctx context.Context, run deployment.RunKey, in semantic.SourceInput, w semantic.Workspace) (out semantic.MatchResult, err error) {
	file, err := w.Syntax(ctx, in)
	if err != nil {
		return out, err
	}
	if file.Source.FileID != in.Source.FileID || file.Source.ContentSHA256 != in.Source.ContentSHA256 {
		return out, fmt.Errorf("%w: syntax does not describe the requested file variant", semantic.ErrIntegrity)
	}
	symbols, err := w.SymbolsByFile(ctx, file.Source.FileID)
	if err != nil {
		return out, err
	}
	keys := map[ir.DeclarationID]*semantic.DeclarationKey{}
	keyCounts := map[string]int{}
	for _, s := range symbols {
		if s.Source == nil || s.Source.FileID != file.Source.FileID {
			return out, fmt.Errorf("%w: symbol %s is not a source symbol of this file", semantic.ErrIntegrity, s.ID)
		}
		if s.Key == nil {
			continue
		}
		key := *s.Key
		keys[s.Source.DeclarationID] = &key
		keyCounts[keyDigest(&key)]++
	}

	previous, err := w.PreviousIdentities(ctx, in.Lineage)
	hasPrevious := err == nil
	if err != nil && !errors.Is(err, semantic.ErrNotFound) {
		return out, err
	}
	// The parser descriptor digest is part of the syntax cache key upstream,
	// so identical bytes prove identical syntax and local IDs.
	sameSyntax := hasPrevious && previous.ContentSHA256 == in.Source.ContentSHA256
	prevByID := map[ir.DeclarationID]semantic.DeclarationIdentity{}
	prevByKey := map[string][]semantic.DeclarationIdentity{}
	// Keyless declarations (parameters, locals, type parameters, anonymous
	// types, initializers) continue by their position among same-kind,
	// same-name declarations of the same owner entity, so an edit elsewhere
	// in the file does not re-mint them or everything they enclose.
	type declKey struct {
		owner string
		kind  ir.DeclarationKind
		name  string
	}
	prevKeyless := map[declKey][]string{}
	if hasPrevious {
		prevEntity := map[ir.DeclarationID]string{}
		for _, d := range previous.Declarations {
			prevByID[d.DeclarationID] = d
			prevEntity[d.DeclarationID] = d.EntityID
			if d.Key != nil {
				digest := keyDigest(d.Key)
				prevByKey[digest] = append(prevByKey[digest], d)
			}
		}
		for _, d := range previous.Declarations {
			if d.Key != nil {
				continue
			}
			owner := in.Lineage
			if d.OwnerID != "" {
				owner = prevEntity[d.OwnerID]
			}
			k := declKey{owner, d.Kind, d.Name}
			prevKeyless[k] = append(prevKeyless[k], d.EntityID)
		}
	}

	claimed := map[string]bool{}
	entities := make(map[ir.DeclarationID]string, len(file.Declarations))
	declarations := make([]semantic.DeclarationIdentity, 0, len(file.Declarations))
	for _, d := range file.Declarations {
		if _, dup := entities[d.ID]; dup {
			return out, fmt.Errorf("%w: duplicate declaration id %s", semantic.ErrIntegrity, d.ID)
		}
		id := semantic.DeclarationIdentity{
			DeclarationID: d.ID,
			EntityID:      graph.ID("entity", run.RepositoryID, run.RunID, in.Lineage, string(d.ID)),
			Kind:          d.Kind,
			Name:          d.Name,
			Span:          d.Span,
			OwnerID:       d.OwnerID,
			Key:           keys[d.ID],
		}
		continued := false
		if sameSyntax {
			prior, ok := prevByID[d.ID]
			if ok && prior.Kind == d.Kind && prior.Name == d.Name && prior.Span == d.Span && prior.OwnerID == d.OwnerID && !claimed[prior.EntityID] {
				id.EntityID = prior.EntityID
				continued = true
			}
		}
		if !continued && id.Key != nil {
			digest := keyDigest(id.Key)
			old := prevByKey[digest]
			if len(old) == 1 && keyCounts[digest] == 1 && !claimed[old[0].EntityID] {
				id.EntityID = old[0].EntityID
				continued = true
			} else if len(old) > 0 {
				out.Ambiguous++
			}
		}
		if !continued && id.Key == nil && !sameSyntax && hasPrevious {
			owner := in.Lineage
			if d.OwnerID != "" {
				owner = entities[d.OwnerID]
			}
			k := declKey{owner, d.Kind, d.Name}
			for len(prevKeyless[k]) > 0 {
				candidate := prevKeyless[k][0]
				prevKeyless[k] = prevKeyless[k][1:]
				if !claimed[candidate] {
					id.EntityID = candidate
					continued = true
					break
				}
			}
		}
		claimed[id.EntityID] = true
		if continued {
			out.Continued++
		} else {
			out.Allocated++
		}
		entities[d.ID] = id.EntityID
		declarations = append(declarations, id)
	}

	// Occurrence continuity. Identical bytes prove correspondence by local ID.
	// After an edit, a site continues when the same entity still contains a
	// site of the same kind spelling the same name, matched in order: the
	// n-th call to foo() inside a continued method keeps its identity even
	// though every byte offset in the file moved.
	prevSites := map[ir.OccurrenceID]semantic.OccurrenceIdentity{}
	type siteKey struct{ entity, kind, name string }
	prevSiteKeys := map[siteKey][]string{}
	if hasPrevious {
		for _, o := range previous.Occurrences {
			if sameSyntax {
				prevSites[o.OccurrenceID] = o
			} else if o.Kind != "" {
				k := siteKey{o.EnclosingEntityID, o.Kind, o.Name}
				prevSiteKeys[k] = append(prevSiteKeys[k], o.PersistentID)
			}
		}
	}
	sites := occurrences(file)
	kinds := siteKinds(file)
	names := siteNames(file)
	seen := make(map[ir.OccurrenceID]bool, len(sites))
	claimedSites := make(map[string]bool, len(sites))
	occ := make([]semantic.OccurrenceIdentity, 0, len(sites))
	for _, o := range sites {
		if seen[o.ID] {
			return out, fmt.Errorf("%w: duplicate occurrence id %s", semantic.ErrIntegrity, o.ID)
		}
		seen[o.ID] = true
		owner := in.Lineage
		if o.EnclosingDeclarationID != "" {
			var ok bool
			if owner, ok = entities[o.EnclosingDeclarationID]; !ok {
				return out, fmt.Errorf("%w: occurrence %s encloses unknown declaration %s", semantic.ErrIntegrity, o.ID, o.EnclosingDeclarationID)
			}
		}
		id := semantic.OccurrenceIdentity{
			OccurrenceID:           o.ID,
			PersistentID:           graph.ID("occurrence", run.RepositoryID, run.RunID, in.Lineage, string(o.ID)),
			EnclosingDeclarationID: o.EnclosingDeclarationID,
			EnclosingEntityID:      owner,
			Kind:                   kinds[o.ID],
			Name:                   names[o.ID],
		}
		continued := false
		if prior, ok := prevSites[o.ID]; ok && prior.EnclosingEntityID == owner && !claimedSites[prior.PersistentID] {
			id.PersistentID = prior.PersistentID
			continued = true
		} else if !sameSyntax {
			k := siteKey{owner, id.Kind, id.Name}
			for len(prevSiteKeys[k]) > 0 {
				candidate := prevSiteKeys[k][0]
				prevSiteKeys[k] = prevSiteKeys[k][1:]
				if !claimedSites[candidate] {
					id.PersistentID = candidate
					continued = true
					break
				}
			}
		}
		claimedSites[id.PersistentID] = true
		if continued {
			out.Continued++
		} else {
			out.Allocated++
		}
		occ = append(occ, id)
	}

	return out, w.PutIdentities(ctx, semantic.FileIdentities{
		Lineage:       in.Lineage,
		FileID:        file.Source.FileID,
		Path:          in.Source.Path,
		ContentSHA256: in.Source.ContentSHA256,
		Declarations:  declarations,
		Occurrences:   occ,
	})
}
