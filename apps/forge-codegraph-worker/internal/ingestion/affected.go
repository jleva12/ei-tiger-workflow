package ingestion

import (
	"context"
	"sort"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/semantic"
	"ei-aitiger-codegraph/worker/internal/repository/github"
)

// changeScope is what a run knows before it consults git: whether everything
// must be recomputed (an initial run, an analysis refresh, a baseline analysed
// under another configuration, or unknown baseline inputs) and which
// compilation contexts have different build inputs than the live generation.
type changeScope struct {
	full        bool
	invalidated map[bc.SourceSetID]bool
}

// contextDigests fingerprints, per compilation context, every input that
// javac attribution depends on besides the source files: the language
// profile, the JDK, the ordered class and module paths with each artifact's
// content digest, generated roots with their tree digests, and the source
// selection patterns. Source roots are covered by per-file content hashes and
// are deliberately excluded, so a source edit alone never changes a digest,
// while a dependency upgrade, a compiler-setting change or a regenerated root
// changes exactly the contexts it touches.
func contextDigests(build bc.BuildContext) map[bc.SourceSetID]string {
	inputs := make(map[bc.InputID]bc.Input, len(build.Inventory.Inputs))
	for _, in := range build.Inventory.Inputs {
		inputs[in.ID] = in
	}
	artifacts := make(map[bc.ArtifactID]bc.Artifact, len(build.Inventory.Artifacts))
	for _, a := range build.Inventory.Artifacts {
		artifacts[a.ID] = a
	}
	jdks := make(map[bc.JDKID]bc.JDK, len(build.Inventory.JDKs))
	for _, j := range build.Inventory.JDKs {
		jdks[j.ID] = j
	}
	type entry struct {
		Kind   string `json:"kind"`
		Ref    string `json:"ref"`
		Digest string `json:"digest,omitempty"`
		Reason string `json:"reason,omitempty"`
	}
	inputEntry := func(kind string, id bc.InputID) entry {
		in := inputs[id]
		return entry{Kind: kind, Ref: string(id), Digest: in.SHA256, Reason: in.UnavailableReason}
	}
	path := func(entries []bc.PathEntry) []entry {
		out := make([]entry, 0, len(entries))
		for _, e := range entries {
			switch e.Kind {
			case bc.EntryArtifact:
				a := artifacts[bc.ArtifactID(e.RefID)]
				in := inputs[a.BinaryInputID]
				out = append(out, entry{Kind: string(e.Kind), Ref: e.RefID, Digest: in.SHA256, Reason: in.UnavailableReason})
			default:
				// Another source set's content is covered by its own files
				// and the downstream expansion; a gap is identified by its ID.
				out = append(out, entry{Kind: string(e.Kind), Ref: e.RefID})
			}
		}
		return out
	}
	out := make(map[bc.SourceSetID]string, len(build.Inventory.SourceSets))
	for _, set := range build.Inventory.SourceSets {
		jdk := jdks[set.JDKID]
		record := struct {
			Language, Version string
			Options           map[string]string
			Release           int
			Preview           bool
			JDK               entry
			JDKVersion        string
			Include, Exclude  []string
			Generated         []entry
			Classpath         []entry
			ModulePath        []entry
		}{
			Language: set.Language, Version: set.LanguageVersion, Options: set.LanguageOptions, Release: set.TargetRelease, Preview: set.EnablePreview,
			JDK: inputEntry("jdk", jdk.HomeInputID), JDKVersion: jdk.Version,
			Include: set.IncludePatterns, Exclude: set.ExcludePatterns,
			Classpath: path(set.Classpath), ModulePath: path(set.ModulePath),
		}
		for _, id := range set.GeneratedRootIDs {
			record.Generated = append(record.Generated, inputEntry("generated_root", id))
		}
		out[set.ID] = "sha256:" + digestOf(record)
	}
	return out
}

// changeSet is the incremental scope of one run: which lineages must be
// re-projected, which compilation contexts must be re-attributed, which
// lineages disappeared, and which new lineages continue an old one.
type changeSet struct {
	full     bool
	contexts map[bc.SourceSetID]bool
	files    map[string]bool // affected lineages
	deleted  []string        // lineages whose file no longer exists
	renamed  map[string]string
}

// dependentsReader is the slice of the graph store the change-set builder
// needs: which entities a file declared, and who points at them.
type dependentsReader interface {
	RecordsByLineage(ctx context.Context, repositoryID, lineage string, generation uint64) ([]graph.Version, error)
	Neighbors(ctx context.Context, q graph.NeighborQuery) (graph.NeighborPage, error)
	LineagesByPath(ctx context.Context, repositoryID, path string, generation uint64) ([]string, error)
}

// computeChangeSet expands git's changed paths to the files and contexts a
// correct incremental run must recompute:
//   - every variant (module × source set) of an added, modified or renamed path;
//   - every file with an open edge into an entity of a changed or deleted file
//     (found through the graph's incoming-edge index);
//   - every compilation context containing one of those files, plus every
//     context whose build inputs changed since the live generation
//     (scope.invalidated), plus every context whose classpath lists such a
//     context's output, transitively.
//
// scope.full, an initial run, or a zero baseline recompute every file and
// context; deletions and renames from git are still honoured so retired
// lineages and continued identities are handled. All files of an affected
// context are re-projected; the diff keeps unchanged ones write-free.
func computeChangeSet(ctx context.Context, repoID string, baseline uint64, inventory []semantic.SourceInput, build bc.BuildContext, changes []github.FileChange, reader dependentsReader, scope changeScope) (changeSet, error) {
	cs := changeSet{contexts: map[bc.SourceSetID]bool{}, files: map[string]bool{}, renamed: map[string]string{}}
	if baseline == 0 {
		cs.full = true
		for _, in := range inventory {
			cs.files[in.Lineage] = true
			cs.contexts[bc.SourceSetID(in.Source.SourceSetID)] = true
		}
		return cs, nil
	}
	byPath := map[string][]semantic.SourceInput{}
	for _, in := range inventory {
		byPath[in.Source.Path] = append(byPath[in.Source.Path], in)
	}
	seedLineages := map[string]bool{}
	for _, c := range changes {
		switch c.Status {
		case github.StatusAdded, github.StatusModified:
			for _, in := range byPath[c.Path] {
				seedLineages[in.Lineage] = true
			}
		case github.StatusRenamed:
			// The old path's lineages come from the stored identity maps: the
			// old file's source sets may no longer exist in this build. A
			// renamed variant continues the old lineage of the same source set
			// when there is one, otherwise the first old lineage.
			olds, err := reader.LineagesByPath(ctx, repoID, c.OldPath, baseline)
			if err != nil {
				return cs, err
			}
			for _, old := range olds {
				cs.deleted = append(cs.deleted, old)
				seedLineages[old] = true
			}
			for _, in := range byPath[c.Path] {
				seedLineages[in.Lineage] = true
				sameSet := graph.Lineage(repoID, in.Source.ModuleID, in.Source.SourceSetID, c.OldPath)
				for _, old := range olds {
					if old == sameSet {
						cs.renamed[in.Lineage] = old
					}
				}
				if _, ok := cs.renamed[in.Lineage]; !ok && len(olds) > 0 {
					cs.renamed[in.Lineage] = olds[0]
				}
			}
		case github.StatusDeleted:
			olds, err := reader.LineagesByPath(ctx, repoID, c.Path, baseline)
			if err != nil {
				return cs, err
			}
			for _, old := range olds {
				cs.deleted = append(cs.deleted, old)
				seedLineages[old] = true
			}
		}
	}
	sort.Strings(cs.deleted)
	if scope.full {
		cs.full = true
		for _, in := range inventory {
			cs.files[in.Lineage] = true
			cs.contexts[bc.SourceSetID(in.Source.SourceSetID)] = true
		}
		return cs, nil
	}
	for id := range scope.invalidated {
		cs.contexts[id] = true
	}
	// Dependents: files whose entities have edges into entities of seed files.
	dependents := map[string]bool{}
	for lineage := range seedLineages {
		versions, err := reader.RecordsByLineage(ctx, repoID, lineage, baseline)
		if err != nil {
			return cs, err
		}
		for _, v := range versions {
			if v.Fact.Node == nil {
				continue
			}
			cursor := ""
			for {
				page, err := reader.Neighbors(ctx, graph.NeighborQuery{RepositoryID: repoID, NodeID: v.Fact.Node.ID, Direction: graph.Incoming, Generation: baseline, Limit: 500, Cursor: cursor})
				if err != nil {
					return cs, err
				}
				for _, n := range page.Neighbors {
					if n.Edge.Lineage != "" && !seedLineages[n.Edge.Lineage] {
						dependents[n.Edge.Lineage] = true
					}
				}
				if page.NextCursor == "" {
					break
				}
				cursor = page.NextCursor
			}
		}
	}
	byLineage := map[string]semantic.SourceInput{}
	for _, in := range inventory {
		byLineage[in.Lineage] = in
	}
	for lineage := range seedLineages {
		if in, ok := byLineage[lineage]; ok {
			cs.contexts[bc.SourceSetID(in.Source.SourceSetID)] = true
		}
	}
	for lineage := range dependents {
		if in, ok := byLineage[lineage]; ok {
			cs.contexts[bc.SourceSetID(in.Source.SourceSetID)] = true
		}
	}
	// Downstream contexts: those whose classpath includes an affected context's output.
	consumers := map[bc.SourceSetID][]bc.SourceSetID{}
	for _, set := range build.Inventory.SourceSets {
		for _, entry := range append(append([]bc.PathEntry(nil), set.Classpath...), set.ModulePath...) {
			if entry.Kind == bc.EntrySourceSet {
				consumers[bc.SourceSetID(entry.RefID)] = append(consumers[bc.SourceSetID(entry.RefID)], set.ID)
			}
		}
	}
	queue := make([]bc.SourceSetID, 0, len(cs.contexts))
	for id := range cs.contexts {
		queue = append(queue, id)
	}
	for len(queue) > 0 {
		id := queue[0]
		queue = queue[1:]
		for _, consumer := range consumers[id] {
			if !cs.contexts[consumer] {
				cs.contexts[consumer] = true
				queue = append(queue, consumer)
			}
		}
	}
	for _, in := range inventory {
		if cs.contexts[bc.SourceSetID(in.Source.SourceSetID)] {
			cs.files[in.Lineage] = true
		}
	}
	sort.Strings(cs.deleted)
	return cs, nil
}

func (cs changeSet) contextList() []bc.SourceSetID {
	out := make([]bc.SourceSetID, 0, len(cs.contexts))
	for id := range cs.contexts {
		out = append(out, id)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// baselineFiles lists the source files open at a generation.
type baselineFiles interface {
	ListNodes(ctx context.Context, q graph.ListQuery) (graph.NodePage, error)
}

// retireMissingFiles adds to a full change set every file lineage open at
// the baseline that the inventory no longer has, so a file deleted while
// the diff was unavailable, or outside it, does not stay in the graph.
func retireMissingFiles(ctx context.Context, store baselineFiles, repoID string, baseline uint64, inventory []semantic.SourceInput, cs *changeSet) error {
	current := make(map[string]bool, len(inventory))
	for _, in := range inventory {
		current[in.Lineage] = true
	}
	deleted := make(map[string]bool, len(cs.deleted))
	for _, lineage := range cs.deleted {
		deleted[lineage] = true
	}
	for _, lineage := range cs.renamed {
		deleted[lineage] = true
	}
	cursor := ""
	for {
		page, err := store.ListNodes(ctx, graph.ListQuery{RepositoryID: repoID, Kind: graph.NodeSourceFile, Generation: baseline, Limit: 500, Cursor: cursor})
		if err != nil {
			return err
		}
		for _, v := range page.Nodes {
			if n := v.Fact.Node; n != nil && !current[n.ID] && !deleted[n.ID] {
				deleted[n.ID] = true
				cs.deleted = append(cs.deleted, n.ID)
			}
		}
		if page.NextCursor == "" || page.NextCursor == cursor {
			break
		}
		cursor = page.NextCursor
	}
	sort.Strings(cs.deleted)
	return nil
}
