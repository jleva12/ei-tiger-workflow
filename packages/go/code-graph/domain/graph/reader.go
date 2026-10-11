package graph

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
