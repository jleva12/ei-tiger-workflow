package organization

import (
	"fmt"
	"slices"
	"strings"
	"time"
	"unicode/utf8"
)

// WorkspaceDocument shares its workspace's access rules and revision. Keeping
// the bounded collection in the entity payload makes document saves atomic with
// workspace edits; stale revisions cannot overwrite another teammate's work.
type WorkspaceDocument struct {
	ID        string    `json:"id"`
	Kind      string    `json:"kind" enum:"feature,document"`
	Title     string    `json:"title" maxLength:"120"`
	Body      string    `json:"body" maxLength:"20000"`
	Status    string    `json:"status" enum:"draft,in_review,ready"`
	CreatedAt time.Time `json:"created_at,omitempty"`
	UpdatedAt time.Time `json:"updated_at,omitempty"`
}

func validateDocuments(documents []WorkspaceDocument) error {
	if len(documents) > 50 {
		return fmt.Errorf("%w: a workspace can hold up to 50 feature plans and documents", ErrInvalid)
	}
	seen := map[string]bool{}
	bytes := 0
	for _, d := range documents {
		if !ValidID(d.ID) || seen[d.ID] || (d.Kind != "feature" && d.Kind != "document") {
			return fmt.Errorf("%w: invalid or duplicate workspace document", ErrInvalid)
		}
		seen[d.ID] = true
		if d.Title == "" || strings.TrimSpace(d.Title) != d.Title || utf8.RuneCountInString(d.Title) > 120 || utf8.RuneCountInString(d.Body) > 20000 {
			return fmt.Errorf("%w: documents need a title of up to 120 characters and content of up to 20,000 characters", ErrInvalid)
		}
		if d.Status != Draft && d.Status != "in_review" && d.Status != "ready" {
			return fmt.Errorf("%w: invalid workspace document status", ErrInvalid)
		}
		bytes += len(d.Title) + len(d.Body)
	}
	if bytes > 250000 {
		return fmt.Errorf("%w: workspace document content exceeds 250 KB", ErrInvalid)
	}
	return nil
}

func stampDocuments(documents, previous []WorkspaceDocument, now time.Time) []WorkspaceDocument {
	out := slices.Clone(documents)
	old := make(map[string]WorkspaceDocument, len(previous))
	for _, d := range previous {
		old[d.ID] = d
	}
	for i, d := range out {
		d.CreatedAt, d.UpdatedAt = now, now
		if before, ok := old[d.ID]; ok {
			d.CreatedAt = before.CreatedAt
			if d.Kind == before.Kind && d.Title == before.Title && d.Body == before.Body && d.Status == before.Status {
				d.UpdatedAt = before.UpdatedAt
			}
		}
		out[i] = d
	}
	return out
}
