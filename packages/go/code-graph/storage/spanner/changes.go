package spannerstore

import (
	"context"
	"fmt"

	"cloud.google.com/go/spanner"

	"ei-aitiger-codegraph/pkg/graph"
)

// ChangeOp classifies what a generation did to a record.
type ChangeOp string

const (
	ChangeAdded   ChangeOp = "added"   // a version opened at the generation and none closed
	ChangeUpdated ChangeOp = "updated" // a version closed at the generation and a new one opened
	ChangeRetired ChangeOp = "retired" // a version closed at the generation with nothing after it
)

// RecordChange is one record a generation touched. Version is the version
// opened at the generation, or for a retired record the version it closed;
// Before is the closed version of an updated record.
type RecordChange struct {
	Op      ChangeOp       `json:"op"`
	ID      string         `json:"id"`
	Version graph.Version  `json:"version"`
	Before  *graph.Version `json:"before,omitempty"`
}

// ChangesPage is one page of a generation's change set with the totals for
// the whole generation, so a caller can size the walk before paging.
type ChangesPage struct {
	Generation uint64         `json:"generation"`
	Commit     string         `json:"commit,omitempty"`
	Added      int64          `json:"added"`
	Updated    int64          `json:"updated"`
	Retired    int64          `json:"retired"`
	Changes    []RecordChange `json:"changes"`
	NextCursor string         `json:"next_cursor,omitempty"`
}

// Changes lists the records of one kind that a published generation added,
// updated or retired, in record id order, through the generation indexes.
// Generation zero means live. Nothing above live is ever exposed.
func (s *Store) Changes(ctx context.Context, repo string, generation uint64, kind graph.RecordKind, limit int, cursor string) (ChangesPage, error) {
	if !validRepositoryID(repo) || (kind != graph.RecordNode && kind != graph.RecordEdge) {
		return ChangesPage{}, fmt.Errorf("%w: changes query", graph.ErrInvalid)
	}
	limit = s.pageLimit(limit)
	t := s.client.ReadOnlyTransaction()
	defer t.Close()
	gen, live, err := s.generationFor(ctx, t, repo, generation)
	if err != nil {
		return ChangesPage{}, err
	}
	if gen == 0 {
		return ChangesPage{}, fmt.Errorf("%w: no generation has been published", graph.ErrNotFound)
	}
	scope := s.cursors.scope("changes", repo, gen, string(kind))
	after, err := s.cursors.decode(scope, gen, cursor)
	if err != nil {
		return ChangesPage{}, err
	}
	page := ChangesPage{Generation: gen, Changes: []RecordChange{}}
	params := map[string]any{"repo": repo, "gen": int64(gen), "kind": string(kind)}
	// Totals over the whole generation: versions opened, versions closed and
	// how many of those closures were retirements.
	it := t.Query(ctx, spanner.Statement{SQL: "SELECT COUNT(*) FROM CGRecords@{FORCE_INDEX=CGRecordsByGenFrom} WHERE RepositoryID=@repo AND GenFrom=@gen AND RecordKind=@kind", Params: params})
	var opened, closed, retired int64
	if err := scanOne(it, &opened); err != nil {
		return ChangesPage{}, err
	}
	it = t.Query(ctx, spanner.Statement{SQL: nullFilteredHint + "SELECT COUNT(*), COUNTIF(Retired) FROM CGRecords@{FORCE_INDEX=CGRecordsByGenTo} WHERE RepositoryID=@repo AND GenTo=@gen AND RecordKind=@kind", Params: params})
	if err := scanTwo(it, &closed, &retired); err != nil {
		return ChangesPage{}, err
	}
	page.Updated, page.Retired = closed-retired, retired
	page.Added = opened - page.Updated
	if page.Added < 0 {
		page.Added = 0
	}

	pageParams := map[string]any{"repo": repo, "gen": int64(gen), "kind": string(kind), "after": after, "limit": int64(limit + 1)}
	openedVersions, err := collectVersions(t.Query(ctx, spanner.Statement{
		SQL:    "SELECT " + recordSelect + " FROM CGRecords@{FORCE_INDEX=CGRecordsByGenFrom} WHERE RepositoryID=@repo AND GenFrom=@gen AND RecordKind=@kind AND RecordID>@after ORDER BY RecordID LIMIT @limit",
		Params: pageParams,
	}), live)
	if err != nil {
		return ChangesPage{}, err
	}
	closedVersions, err := collectVersions(t.Query(ctx, spanner.Statement{
		SQL:    nullFilteredHint + "SELECT " + recordSelect + " FROM CGRecords@{FORCE_INDEX=CGRecordsByGenTo} WHERE RepositoryID=@repo AND GenTo=@gen AND RecordKind=@kind AND RecordID>@after ORDER BY RecordID LIMIT @limit",
		Params: pageParams,
	}), live)
	if err != nil {
		return ChangesPage{}, err
	}
	// Merge the two streams by record id. Both are cut at limit+1, so an id
	// beyond the shorter stream's last id may lack its counterpart; stop at
	// the smaller last id to keep pairs together, and never emit more than
	// limit entries.
	bound := ""
	if len(openedVersions) > limit {
		bound = openedVersions[limit].Fact.Key().ID
	}
	if len(closedVersions) > limit {
		if id := closedVersions[limit].Fact.Key().ID; bound == "" || id < bound {
			bound = id
		}
	}
	i, j := 0, 0
	for i < len(openedVersions) || j < len(closedVersions) {
		var change RecordChange
		switch {
		case j >= len(closedVersions) || (i < len(openedVersions) && openedVersions[i].Fact.Key().ID < closedVersions[j].Fact.Key().ID):
			v := openedVersions[i]
			change = RecordChange{Op: ChangeAdded, ID: v.Fact.Key().ID, Version: v}
			i++
		case i >= len(openedVersions) || closedVersions[j].Fact.Key().ID < openedVersions[i].Fact.Key().ID:
			v := closedVersions[j]
			change = RecordChange{Op: ChangeRetired, ID: v.Fact.Key().ID, Version: v}
			j++
		default:
			before := closedVersions[j]
			change = RecordChange{Op: ChangeUpdated, ID: openedVersions[i].Fact.Key().ID, Version: openedVersions[i], Before: &before}
			i++
			j++
		}
		if bound != "" && change.ID >= bound {
			page.NextCursor = s.cursors.encode(scope, gen, lastID(page.Changes, after))
			return page, nil
		}
		if page.Commit == "" {
			if change.Op == ChangeRetired {
				page.Commit = change.Version.CommitTo
			} else {
				page.Commit = change.Version.CommitFrom
			}
		}
		page.Changes = append(page.Changes, change)
		if len(page.Changes) == limit {
			if i < len(openedVersions) || j < len(closedVersions) {
				page.NextCursor = s.cursors.encode(scope, gen, change.ID)
			}
			return page, nil
		}
	}
	return page, nil
}

func lastID(changes []RecordChange, fallback string) string {
	if len(changes) == 0 {
		return fallback
	}
	return changes[len(changes)-1].ID
}

func scanOne(it *spanner.RowIterator, out *int64) error {
	defer it.Stop()
	row, err := it.Next()
	if err != nil {
		return err
	}
	return row.Columns(out)
}

func scanTwo(it *spanner.RowIterator, a, b *int64) error {
	defer it.Stop()
	row, err := it.Next()
	if err != nil {
		return err
	}
	return row.Columns(a, b)
}
