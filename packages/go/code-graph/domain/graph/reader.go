package graph

import "context"

// RepositoryState is the read gate. Readers see the versions open at
// LiveGeneration; a generation still loading is above it and invisible.
type RepositoryState struct {
	RepositoryID   string `json:"repository_id"`
	Branch         string `json:"branch,omitempty"`
	LiveGeneration uint64 `json:"live_generation"`
	LiveCommit     string `json:"live_commit,omitempty"`
	LiveRunID      string `json:"live_run_id,omitempty"`
}

type Direction string

const (
	Outgoing Direction = "out"
	Incoming Direction = "in"
	Both     Direction = "both"
)

// Generation zero in a query means "the live generation".
type ListQuery struct {
	RepositoryID string
	Kind         string // node kind or edge kind filter; empty means all
	Generation   uint64
	Limit        int
	Cursor       string
}

type NodePage struct {
	Generation uint64    `json:"generation"`
	Nodes      []Version `json:"nodes"`
	NextCursor string    `json:"next_cursor,omitempty"`
}

type EdgePage struct {
	Generation uint64    `json:"generation"`
	Edges      []Version `json:"edges"`
	NextCursor string    `json:"next_cursor,omitempty"`
}

type NeighborQuery struct {
	RepositoryID string
	NodeID       string
	Direction    Direction
	EdgeKinds    []string // empty means all
	Generation   uint64
	Limit        int
	Cursor       string
}

type Neighbor struct {
	Edge Version  `json:"edge"`
	Node *Version `json:"node,omitempty"` // the far endpoint, when it exists at the generation
}

type NeighborPage struct {
	Generation uint64     `json:"generation"`
	Neighbors  []Neighbor `json:"neighbors"`
	NextCursor string     `json:"next_cursor,omitempty"`
}

// Reader is the query surface an agent-facing API builds on. Every method
// resolves Generation zero to the live generation and never exposes versions
// above it.
type Reader interface {
	State(ctx context.Context, repositoryID string) (RepositoryState, error)
	GetNode(ctx context.Context, repositoryID, id string, generation uint64) (Version, error)
	GetEdge(ctx context.Context, repositoryID, id string, generation uint64) (Version, error)
	ListNodes(ctx context.Context, q ListQuery) (NodePage, error)
	ListEdges(ctx context.Context, q ListQuery) (EdgePage, error)
	Neighbors(ctx context.Context, q NeighborQuery) (NeighborPage, error)
	// History returns every version of a record, oldest first.
	History(ctx context.Context, repositoryID string, key Key) ([]Version, error)
	// RecordsByLineage returns the versions open at a generation for one file lineage.
	RecordsByLineage(ctx context.Context, repositoryID, lineage string, generation uint64) ([]Version, error)
}
