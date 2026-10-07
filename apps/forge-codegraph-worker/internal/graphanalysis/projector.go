package graphanalysis

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"

	"ei-aitiger-codegraph/pkg/deployment"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

// maxSymbolDepth bounds constructed/derived symbol nesting during entity
// resolution so a cyclic symbol table cannot recurse without end.
const maxSymbolDepth = 256

// maxCandidateTargets bounds the candidates an unresolved reference lists;
// candidate_count keeps the full number. A call of an overloaded name in a
// generated API can have thousands.
const maxCandidateTargets = 256

// Projector emits canonical nodes and edges from identities and lookups.
//
// Facts that have no lineage (external, intrinsic, constructed and derived
// symbols) are emitted first, once per run, and carry no source anchor; the
// Differ routes anchor-less facts to its lineage-less path. Facts of an
// affected file follow, in file order, every one anchored at that file's
// lineage and content hash. Resolved lookups become direct edges; only
// occurrences without any lookup get a standalone site node.
type Projector struct{}

var _ semantic.Projector = Projector{}

func (Projector) Project(ctx context.Context, in semantic.ProjectRequest, w semantic.Workspace, emit semantic.Emit) (semantic.ProjectionResult, error) {
	if w == nil || emit == nil || in.Run.Validate() != nil {
		return semantic.ProjectionResult{}, fmt.Errorf("%w: run key, workspace and emit required", semantic.ErrInvalid)
	}
	p := &projection{run: in.Run, w: w, emit: emit, entities: map[string]string{}}
	err := w.EachSymbol(ctx, func(s semantic.Symbol) error {
		if s.Source != nil || s.Module != nil {
			return nil
		}
		if err := p.projectSymbol(ctx, s); err != nil {
			return fmt.Errorf("project symbol %s: %w", s.ID, err)
		}
		return nil
	})
	if err != nil {
		return p.result, err
	}
	for _, f := range in.Files {
		if err := ctx.Err(); err != nil {
			return p.result, err
		}
		if !graph.ValidID(f.Lineage) {
			return p.result, fmt.Errorf("%w: file %s has no lineage", semantic.ErrInvalid, f.Source.Path)
		}
		if err := p.projectFile(ctx, f); err != nil {
			return p.result, fmt.Errorf("project %s: %w", f.Source.Path, err)
		}
		if in.Progress != nil {
			in.Progress()
		}
	}
	return p.result, nil
}

type projection struct {
	run      deployment.RunKey
	w        semantic.Workspace
	emit     semantic.Emit
	entities map[string]string // symbol ID -> entity ID, for the whole run
	result   semantic.ProjectionResult
	// lookupNodes counts, per base ID, the unresolved_reference nodes of the
	// current file: a site can hold several lookups of one kind (a method
	// overriding several supertypes' methods), and each keeps its own node.
	lookupNodes map[string]int
}

func (p *projection) node(ctx context.Context, n graph.Node) error {
	p.result.Nodes++
	cleanNode(&n)
	return p.emit(ctx, graph.Fact{Node: &n})
}

func (p *projection) edge(ctx context.Context, kind, source, target, site string, a *graph.SourceAnchor, properties map[string]graph.PropertyValue) error {
	p.result.Edges++
	cleanProperties(properties)
	e := graph.Edge{ID: graph.ID("edge", kind, source, target, site), Kind: kind, SourceID: source, TargetID: target, Source: a, Properties: properties}
	return p.emit(ctx, graph.Fact{Edge: &e})
}

// symbolEntity resolves a run-local symbol to its persistent entity ID.
func (p *projection) symbolEntity(ctx context.Context, s semantic.Symbol, depth int) (string, error) {
	if id, ok := p.entities[s.ID]; ok {
		return id, nil
	}
	if err := ctx.Err(); err != nil {
		return "", err
	}
	if depth > maxSymbolDepth {
		return "", fmt.Errorf("%w: symbol %s nests deeper than %d", semantic.ErrIntegrity, s.ID, maxSymbolDepth)
	}
	var id string
	switch {
	case s.Module != nil:
		file, err := p.w.File(ctx, s.Module.FileID)
		if err != nil {
			return "", err
		}
		id = file.Lineage
	case s.Source != nil:
		entity, err := p.w.Entity(ctx, s.Source.FileID, s.Source.DeclarationID)
		if err != nil {
			return "", err
		}
		id = entity
	case s.Intrinsic != nil:
		id = graph.ID("intrinsic", s.Intrinsic.Language, s.Intrinsic.Name, s.Intrinsic.DefinitionDigest)
	case s.External != nil:
		if s.Key == nil {
			return "", fmt.Errorf("%w: external symbol %s has no canonical key", semantic.ErrIntegrity, s.ID)
		}
		id = graph.ID("external", p.run.RepositoryID, s.External.ArtifactID, s.External.ArtifactFingerprint, CanonicalKey(s.Key))
	case s.Constructed != nil:
		c := s.Constructed
		parts := []string{p.run.RepositoryID, c.Language, c.Kind, c.CanonicalSignature, c.DefinitionDigest}
		components, _, err := p.relatedEntities(ctx, c.ComponentSymbolIDs, depth, false)
		if err != nil {
			return "", err
		}
		id = graph.ID("constructed", append(parts, components...)...)
	case s.Derived != nil:
		if s.Key == nil {
			return "", fmt.Errorf("%w: derived symbol %s has no canonical key", semantic.ErrIntegrity, s.ID)
		}
		d := s.Derived
		parts := []string{p.run.RepositoryID, d.Language, d.Rule, d.DefinitionDigest, CanonicalKey(s.Key)}
		contributors, _, err := p.relatedEntities(ctx, d.SourceSymbolIDs, depth, true)
		if err != nil {
			return "", err
		}
		id = graph.ID("derived", append(parts, contributors...)...)
	default:
		return "", fmt.Errorf("%w: symbol %s has no identity", semantic.ErrIntegrity, s.ID)
	}
	p.entities[s.ID] = id
	return id, nil
}

// relatedEntities resolves the symbols a constructed or derived symbol is
// built from, in the order they are listed. sourceOnly requires declarations.
func (p *projection) relatedEntities(ctx context.Context, ids []string, depth int, sourceOnly bool) ([]string, []semantic.Symbol, error) {
	if len(ids) == 0 {
		return nil, nil, nil
	}
	symbols, err := p.w.Symbols(ctx, ids)
	if err != nil {
		return nil, nil, err
	}
	entities := make([]string, 0, len(ids))
	related := make([]semantic.Symbol, 0, len(ids))
	for _, id := range ids {
		s, ok := symbols[id]
		if !ok {
			return nil, nil, fmt.Errorf("%w: related symbol %s is missing", semantic.ErrIntegrity, id)
		}
		if sourceOnly && s.Source == nil {
			return nil, nil, fmt.Errorf("%w: derived symbol contributor %s is not a declaration", semantic.ErrIntegrity, id)
		}
		entity, err := p.symbolEntity(ctx, s, depth+1)
		if err != nil {
			return nil, nil, err
		}
		entities = append(entities, entity)
		related = append(related, s)
	}
	return entities, related, nil
}

// projectSymbol emits the lineage-less node of a non-source symbol together
// with its derived_from and type_component edges.
func (p *projection) projectSymbol(ctx context.Context, s semantic.Symbol) error {
	id, err := p.symbolEntity(ctx, s, 0)
	if err != nil {
		return err
	}
	n := graph.Node{ID: id, Kind: graph.NodeExternalSymbol, Name: s.Name, Properties: map[string]graph.PropertyValue{}}
	if s.Key != nil {
		n.Name = s.Key.Name
		n.QualifiedName = s.Key.CanonicalSignature
		n.Properties["declaration_kind"] = graph.StringValue(string(s.Key.Kind))
		n.Properties["owner_key"] = graph.StringValue(s.Key.OwnerKey)
		n.Properties["canonical_signature"] = graph.StringValue(s.Key.CanonicalSignature)
		if s.External != nil {
			n.Properties["artifact_id"] = graph.StringValue(s.External.ArtifactID)
			n.Properties["artifact_fingerprint"] = graph.StringValue(s.External.ArtifactFingerprint)
		}
	}
	switch {
	case s.Intrinsic != nil:
		n.Kind = graph.NodeIntrinsic
		if s.Key != nil {
			n.Kind = graph.NodeIntrinsic + "_" + string(s.Key.Kind)
		}
		n.Name = s.Intrinsic.Name
		n.Properties = map[string]graph.PropertyValue{"language": graph.StringValue(s.Intrinsic.Language), "definition_digest": graph.StringValue(s.Intrinsic.DefinitionDigest)}
	case s.Constructed != nil:
		n.Kind = graph.NodeConstructedType
		n.Name = s.Constructed.CanonicalSignature
		n.QualifiedName = s.Constructed.CanonicalSignature
		n.Properties = map[string]graph.PropertyValue{"language": graph.StringValue(s.Constructed.Language), "type_kind": graph.StringValue(s.Constructed.Kind)}
	case s.Derived != nil:
		n.Kind = "derived_" + string(s.Key.Kind)
		n.Properties["derivation_rule"] = graph.StringValue(s.Derived.Rule)
	}
	if err := p.node(ctx, n); err != nil {
		return err
	}
	if s.Derived != nil {
		// Contributor evidence lives on the contributor node; the edge itself
		// stays lineage-less so it is never retired with a contributor's file.
		contributors, _, err := p.relatedEntities(ctx, s.Derived.SourceSymbolIDs, 0, true)
		if err != nil {
			return err
		}
		for _, target := range contributors {
			if err := p.edge(ctx, graph.EdgeDerivedFrom, id, target, "", nil, nil); err != nil {
				return err
			}
		}
	}
	if s.Constructed != nil {
		components, _, err := p.relatedEntities(ctx, s.Constructed.ComponentSymbolIDs, 0, false)
		if err != nil {
			return err
		}
		for i, target := range components {
			if err := p.edge(ctx, graph.EdgeTypeComponent, id, target, fmt.Sprint(i), nil, nil); err != nil {
				return err
			}
		}
	}
	return nil
}

func (p *projection) projectFile(ctx context.Context, in semantic.SourceInput) error {
	file, err := p.w.Syntax(ctx, in)
	if err != nil {
		return err
	}
	if file.Source.FileID != in.Source.FileID || file.Source.ContentSHA256 != in.Source.ContentSHA256 {
		return fmt.Errorf("%w: syntax does not describe the requested file variant", semantic.ErrIntegrity)
	}
	content, err := p.w.SourceBytes(ctx, in)
	if err != nil {
		return err
	}
	if sum := sha256.Sum256(content); hex.EncodeToString(sum[:]) != in.Source.ContentSHA256 {
		return fmt.Errorf("%w: source bytes do not match the file's content hash", semantic.ErrIntegrity)
	}
	identities, err := p.w.Identities(ctx, file.Source.FileID)
	if err != nil {
		return err
	}
	if identities.Lineage != in.Lineage || identities.ContentSHA256 != in.Source.ContentSHA256 {
		return fmt.Errorf("%w: identities describe another file variant", semantic.ErrIntegrity)
	}
	lookups, err := p.w.Lookups(ctx, file.Source.FileID)
	if err != nil {
		return err
	}
	declarations := make(map[ir.DeclarationID]semantic.DeclarationIdentity, len(identities.Declarations))
	for _, d := range identities.Declarations {
		declarations[d.DeclarationID] = d
	}
	sites := make(map[ir.OccurrenceID]semantic.OccurrenceIdentity, len(identities.Occurrences))
	for _, o := range identities.Occurrences {
		sites[o.OccurrenceID] = o
	}

	fa := fileAnchor(in)
	if err := p.node(ctx, graph.Node{ID: in.Lineage, Kind: graph.NodeSourceFile, Name: in.Source.Path, Source: &fa, Properties: map[string]graph.PropertyValue{
		"language":      graph.StringValue(in.Source.Language),
		"module_id":     graph.StringValue(in.Source.ModuleID),
		"source_set_id": graph.StringValue(in.Source.SourceSetID),
		"file_path":     graph.StringValue(in.Source.Path),
	}}); err != nil {
		return err
	}

	members := containerMembers(file.Declarations)
	lines := &lineIndex{content: content}
	chunkEmit := func(ctx context.Context, f graph.Fact) error {
		if f.Node != nil {
			p.result.Nodes++
		} else {
			p.result.Edges++
		}
		return p.emit(ctx, f)
	}
	for _, d := range file.Declarations {
		id, ok := declarations[d.ID]
		if !ok {
			return fmt.Errorf("%w: declaration %s has no identity", semantic.ErrIntegrity, d.ID)
		}
		a := anchor(in, d.Span)
		n := graph.Node{ID: id.EntityID, Kind: string(d.Kind), Name: d.Name, Source: &a}
		if id.Key != nil {
			n.QualifiedName = id.Key.CanonicalSignature
		}
		searchProperties(&n, in.Source, d, id.Key)
		if list := members[d.ID]; list != "" {
			n.Properties["members"] = graph.StringValue(list)
		}
		cleanNode(&n) // chunks copy the name
		if err := retainDeclarationText(ctx, &n, in, d, lines, chunkEmit); err != nil {
			return err
		}
		cleanNode(&n)
		sealSearchDocument(&n)
		if err := p.node(ctx, n); err != nil {
			return err
		}
		owner := in.Lineage
		if d.OwnerID != "" {
			o, ok := declarations[d.OwnerID]
			if !ok {
				return fmt.Errorf("%w: declaration %s has unknown owner %s", semantic.ErrIntegrity, d.ID, d.OwnerID)
			}
			owner = o.EntityID
		}
		if err := p.edge(ctx, graph.EdgeContains, owner, id.EntityID, "", &a, nil); err != nil {
			return err
		}
	}

	kinds := siteKinds(file)
	p.lookupNodes = map[string]int{}
	covered := make(map[ir.OccurrenceID]bool, len(lookups))
	for _, l := range lookups {
		if l.OccurrenceID != "" {
			covered[l.OccurrenceID] = true
		}
	}
	for _, o := range occurrences(file) {
		if covered[o.ID] {
			continue
		}
		site, ok := sites[o.ID]
		if !ok {
			return fmt.Errorf("%w: occurrence %s has no identity", semantic.ErrIntegrity, o.ID)
		}
		a := anchor(in, o.Span)
		if err := p.node(ctx, graph.Node{ID: site.PersistentID, Kind: kinds[o.ID], Source: &a}); err != nil {
			return err
		}
		if err := p.edge(ctx, graph.EdgeContains, site.EnclosingEntityID, site.PersistentID, "", &a, nil); err != nil {
			return err
		}
	}
	for _, l := range lookups {
		if err := p.projectLookup(ctx, in, file.Source.FileID, l, declarations, sites, kinds); err != nil {
			return fmt.Errorf("lookup %s: %w", l.ID, err)
		}
	}
	return nil
}

// projectLookup emits one resolved edge or one unresolved_reference node for
// a lookup. Occurrence lookups are sited at the occurrence's enclosing entity;
// declaration lookups (overrides) are sited at the declaration itself.
func (p *projection) projectLookup(ctx context.Context, in semantic.SourceInput, fileID ir.FileID, l semantic.Lookup, declarations map[ir.DeclarationID]semantic.DeclarationIdentity, sites map[ir.OccurrenceID]semantic.OccurrenceIdentity, kinds map[ir.OccurrenceID]string) error {
	if l.FileID != fileID {
		return fmt.Errorf("%w: lookup belongs to file %s", semantic.ErrIntegrity, l.FileID)
	}
	if (l.Evidence.Lineage != "" && l.Evidence.Lineage != in.Lineage) || (l.Evidence.ContentSHA256 != "" && l.Evidence.ContentSHA256 != in.Source.ContentSHA256) {
		return fmt.Errorf("%w: lookup evidence points at another file variant", semantic.ErrIntegrity)
	}
	a := anchor(in, l.Evidence.Span)
	var source, site string
	properties := map[string]graph.PropertyValue{"status": graph.StringValue(string(l.Status))}
	if l.Provenance != "" {
		properties["provenance"] = graph.StringValue(l.Provenance)
	}
	switch {
	case l.OccurrenceID != "":
		o, ok := sites[l.OccurrenceID]
		if !ok {
			return fmt.Errorf("%w: occurrence %s has no identity", semantic.ErrIntegrity, l.OccurrenceID)
		}
		source, site = o.EnclosingEntityID, o.PersistentID
		properties["occurrence_id"] = graph.StringValue(o.PersistentID)
		properties["occurrence_kind"] = graph.StringValue(kinds[l.OccurrenceID])
	case l.DeclarationID != "":
		d, ok := declarations[l.DeclarationID]
		if !ok {
			return fmt.Errorf("%w: declaration %s has no identity", semantic.ErrIntegrity, l.DeclarationID)
		}
		source, site = d.EntityID, ""
	default:
		return fmt.Errorf("%w: lookup has neither an occurrence nor a declaration site", semantic.ErrIntegrity)
	}

	if l.Status != semantic.LookupResolved {
		if l.CandidateRole != "" {
			properties["candidate_role"] = graph.StringValue(l.CandidateRole)
			properties["has_unknown_candidates"] = graph.BoolValue(l.HasUnknownCandidates)
			targets := make([]string, 0, min(len(l.CandidateIDs), maxCandidateTargets))
			seen := make(map[string]bool, cap(targets))
			for i, symbolID := range l.CandidateIDs {
				if len(targets) == maxCandidateTargets {
					properties["candidate_count"] = graph.Int64Value(int64(len(targets) + len(l.CandidateIDs) - i))
					break
				}
				target, err := p.targetEntity(ctx, symbolID)
				if err != nil {
					return err
				}
				if !seen[target] {
					seen[target] = true
					targets = append(targets, target)
				}
			}
			properties["candidate_target_ids"] = graph.StringsValue(targets)
		}
		anchorSite := site
		if anchorSite == "" {
			anchorSite = source
		}
		id := graph.ID("lookup", anchorSite, string(l.Kind))
		if ordinal := p.lookupNodes[id]; ordinal > 0 {
			p.lookupNodes[id]++
			id = graph.ID("lookup", anchorSite, string(l.Kind), fmt.Sprint(ordinal))
		} else {
			p.lookupNodes[id] = 1
		}
		n := graph.Node{ID: id, Kind: graph.NodeUnresolvedReference, Source: &a, Properties: properties}
		properties["lookup_kind"] = graph.StringValue(string(l.Kind))
		if l.Reason != "" {
			properties["reason"] = graph.StringValue(l.Reason)
		}
		if l.Cause != "" {
			properties["cause"] = graph.StringValue(string(l.Cause))
		}
		if l.DiagnosticCode != "" {
			properties["diagnostic_code"] = graph.StringValue(l.DiagnosticCode)
		}
		if err := p.node(ctx, n); err != nil {
			return err
		}
		return p.edge(ctx, graph.EdgeContains, source, n.ID, "", &a, nil)
	}

	if l.SelectedSymbolID == "" {
		return fmt.Errorf("%w: resolved lookup selects no symbol", semantic.ErrIntegrity)
	}
	kind := l.Kind.EdgeKind()
	if kind == "" {
		return fmt.Errorf("%w: lookup kind %q has no edge", semantic.ErrIntegrity, l.Kind)
	}
	target, err := p.targetEntity(ctx, l.SelectedSymbolID)
	if err != nil {
		return err
	}
	return p.edge(ctx, kind, source, target, site, &a, properties)
}

func (p *projection) targetEntity(ctx context.Context, symbolID string) (string, error) {
	if id, ok := p.entities[symbolID]; ok {
		return id, nil
	}
	s, err := p.w.Symbol(ctx, symbolID)
	if err != nil {
		return "", err
	}
	return p.symbolEntity(ctx, s, 0)
}
