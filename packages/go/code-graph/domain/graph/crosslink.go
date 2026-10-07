package graph

import (
	"fmt"
	"strings"
)

// Cross-repository link kinds: how a node in one repository's graph reaches
// a node in another's outside the code any one compiler sees.
const (
	CrossCallsAPI   = "calls_api"   // calls its API over the network: HTTP, gRPC, GraphQL
	CrossSendsEvent = "sends_event" // publishes an event or message it consumes
	CrossDependsOn  = "depends_on"  // uses it as a library or package
	CrossSharesData = "shares_data" // reads or writes the same database or storage
	CrossConnectsTo = "connects_to" // connected, without saying how
)

// CrossManual is the provenance of a link someone drew by hand.
const CrossManual = "manual"

var crossKinds = map[string]bool{CrossCallsAPI: true, CrossSendsEvent: true, CrossDependsOn: true, CrossSharesData: true, CrossConnectsTo: true}

// ValidCrossKind reports whether kind is a cross-repository link kind.
func ValidCrossKind(kind string) bool { return crossKinds[kind] }

// CrossEnd is one end of a cross-repository link: a node of a repository's
// graph. The node's qualified name and kind are kept with its ID, so the link
// finds the node again when an ingestion gives it a new ID (a graph rebuilt
// without a baseline, or an overload made ambiguous).
type CrossEnd struct {
	RepositoryID  string `json:"repository_id"`
	NodeID        string `json:"node_id"`
	QualifiedName string `json:"qualified_name,omitempty"`
	Kind          string `json:"kind,omitempty"`
}

// CrossLink is an edge from a node in one repository's graph to a node in
// another's: the method that calls an API to the handler that serves it, or
// the one that publishes an event to the listener that consumes it. Each
// repository's graph is versioned on its own, so a link belongs to neither:
// it is kept beside them and followed at each repository's live generation.
// Owner is who keeps the link, e.g. team:<id>, which replaces its links as
// one set.
type CrossLink struct {
	ID         string   `json:"id"`
	Owner      string   `json:"owner"`
	Kind       string   `json:"kind"`
	Source     CrossEnd `json:"source"`
	Target     CrossEnd `json:"target"`
	Label      string   `json:"label,omitempty"` // e.g. POST /v1/orders, or the topic
	Provenance string   `json:"provenance"`
	CreatedBy  string   `json:"created_by,omitempty"`
}

// ValidCrossToken reports whether a link or owner ID is a short printable
// token without spaces: a UUID, or team:<uuid>.
func ValidCrossToken(s string) bool {
	if s == "" || len(s) > 128 {
		return false
	}
	for _, c := range s {
		if c <= ' ' || c > '~' {
			return false
		}
	}
	return true
}

// validRepository accepts a repository ID the way the store does: a
// printable identifier without spaces, such as repo:<token>.
func validRepository(id string) bool {
	if id == "" || len(id) > 256 {
		return false
	}
	for _, c := range id {
		if c <= ' ' || c > '~' {
			return false
		}
	}
	return true
}

func (e CrossEnd) validate(side string) error {
	if !validRepository(e.RepositoryID) || !ValidID(e.NodeID) {
		return fmt.Errorf("%w: cross link %s repository and node ids", ErrInvalid, side)
	}
	if err := text("cross link "+side+" qualified name", e.QualifiedName, false, 4096); err != nil {
		return err
	}
	return text("cross link "+side+" kind", e.Kind, false, 64)
}

func (l CrossLink) Validate() error {
	if !ValidCrossToken(l.ID) || !ValidCrossToken(l.Owner) {
		return fmt.Errorf("%w: cross link id and owner", ErrInvalid)
	}
	if !ValidCrossKind(l.Kind) {
		return fmt.Errorf("%w: cross link kind %q", ErrInvalid, l.Kind)
	}
	if err := l.Source.validate("source"); err != nil {
		return err
	}
	if err := l.Target.validate("target"); err != nil {
		return err
	}
	if l.Source.RepositoryID == l.Target.RepositoryID {
		return fmt.Errorf("%w: cross link within one repository", ErrInvalid)
	}
	if l.Provenance != CrossManual {
		return fmt.Errorf("%w: cross link provenance %q", ErrInvalid, l.Provenance)
	}
	if err := text("cross link label", l.Label, false, 500); err != nil {
		return err
	}
	return text("cross link created_by", l.CreatedBy, false, 255)
}

// End returns the link's end on one side: its source for Outgoing, its
// target for Incoming.
func (l CrossLink) End(dir Direction) CrossEnd {
	if dir == Incoming {
		return l.Target
	}
	return l.Source
}

// Far returns the end opposite the side a walk arrives from: the target
// when following a link out of its source, the source when following it
// back from its target.
func (l CrossLink) Far(dir Direction) CrossEnd {
	if dir == Incoming {
		return l.Source
	}
	return l.Target
}

// CrossLinkQuery selects the links touching a repository: those whose
// source (Outgoing) or target (Incoming) is there, or either (Both). With
// NodeIDs or QualifiedNames, only links whose end on that side is one of
// those nodes, matched by ID or by qualified name; without either, every
// link on that side.
type CrossLinkQuery struct {
	RepositoryID   string
	Direction      Direction
	NodeIDs        []string
	QualifiedNames []string
}

func (q CrossLinkQuery) Validate() error {
	if !validRepository(q.RepositoryID) {
		return fmt.Errorf("%w: cross link query repository", ErrInvalid)
	}
	switch q.Direction {
	case Outgoing, Incoming, Both:
	default:
		return fmt.Errorf("%w: cross link query direction %q", ErrInvalid, q.Direction)
	}
	for _, id := range q.NodeIDs {
		if !ValidID(id) {
			return fmt.Errorf("%w: cross link query node id", ErrInvalid)
		}
	}
	for _, name := range q.QualifiedNames {
		if strings.TrimSpace(name) == "" || len(name) > 4096 {
			return fmt.Errorf("%w: cross link query qualified name", ErrInvalid)
		}
	}
	return nil
}

// Matches reports whether the link's end on dir's side answers the query's
// nodes; a query without nodes matches every link on that side.
func (q CrossLinkQuery) Matches(l CrossLink, dir Direction) bool {
	end := l.End(dir)
	if end.RepositoryID != q.RepositoryID {
		return false
	}
	if len(q.NodeIDs) == 0 && len(q.QualifiedNames) == 0 {
		return true
	}
	for _, id := range q.NodeIDs {
		if end.NodeID == id {
			return true
		}
	}
	for _, name := range q.QualifiedNames {
		if end.QualifiedName != "" && end.QualifiedName == name {
			return true
		}
	}
	return false
}
