package organization

import (
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"testing"
	"time"
)

func TestWorkspaceDocumentsPersistAndRejectStaleEdits(t *testing.T) {
	records := slices.DeleteFunc(fixture(), func(e Entity) bool { return e.Kind == Repository || e.Kind == Responsibility })
	now := time.Now().UTC()
	w := Entity{ID: "planning", TenantID: "usp", PlatformID: "cirrus", ParentID: "cirrus", Kind: Workspace, Name: "Planning", Slug: "planning", Status: Active, ScopeMode: SelectedTeams, TeamIDs: []string{"core"}, Documents: []WorkspaceDocument{
		{ID: "feature-1", Kind: "feature", Title: "Search filters", Body: "Goal\nFilter by repository", Status: Draft},
		{ID: "doc-1", Kind: "document", Title: "Design decisions", Body: "Use the existing query API.", Status: "ready"},
	}}
	saved, err := Prepare(records, w, false, now)
	if err != nil {
		t.Fatal(err)
	}
	// The storage layer persists the entity as JSON, with no separate schema.
	payload, err := json.Marshal(saved)
	if err != nil {
		t.Fatal(err)
	}
	var loaded Entity
	if err = json.Unmarshal(payload, &loaded); err != nil {
		t.Fatal(err)
	}
	if len(loaded.Documents) != 2 || loaded.Documents[0].Body != w.Documents[0].Body || !loaded.Documents[0].CreatedAt.Equal(now) {
		t.Fatalf("documents not preserved: %+v", loaded)
	}
	records = append(records, loaded)
	loaded.Documents = slices.Clone(loaded.Documents)
	loaded.Documents[0].Body = "Revised proposal"
	loaded.Documents[0].UpdatedAt = now.Add(24 * time.Hour) // client timestamps are not trusted
	updated, err := Prepare(records, loaded, false, now.Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	if !updated.Documents[0].CreatedAt.Equal(now) || !updated.Documents[0].UpdatedAt.Equal(now.Add(time.Minute)) || !updated.Documents[1].UpdatedAt.Equal(now) {
		t.Fatalf("incorrect timestamps: %+v", updated.Documents)
	}
	records[len(records)-1] = updated
	if _, err = Prepare(records, saved, false, now); !errors.Is(err, ErrConflict) {
		t.Fatalf("stale document edit accepted: %v", err)
	}
	updated.Documents = updated.Documents[1:]
	if saved, err = Prepare(records, updated, false, now); err != nil || len(saved.Documents) != 1 {
		t.Fatalf("document removal failed: %+v %v", saved, err)
	}
}

func TestWorkspaceDocumentValidation(t *testing.T) {
	doc := WorkspaceDocument{ID: "plan", Kind: "feature", Title: "Proposal", Status: Draft}
	for _, mutate := range []func(*WorkspaceDocument){
		func(d *WorkspaceDocument) { d.ID = "../plan" },
		func(d *WorkspaceDocument) { d.Kind = "repository" },
		func(d *WorkspaceDocument) { d.Title = " " },
		func(d *WorkspaceDocument) { d.Title = strings.Repeat("a", 121) },
		func(d *WorkspaceDocument) { d.Body = strings.Repeat("a", 20001) },
		func(d *WorkspaceDocument) { d.Status = "published" },
	} {
		invalid := doc
		mutate(&invalid)
		if err := validateDocuments([]WorkspaceDocument{invalid}); !errors.Is(err, ErrInvalid) {
			t.Fatalf("accepted invalid document: %+v", invalid)
		}
	}
	if err := validateDocuments([]WorkspaceDocument{doc, doc}); !errors.Is(err, ErrInvalid) {
		t.Fatal("duplicate IDs accepted")
	}
	if err := validateDocuments(make([]WorkspaceDocument, 51)); !errors.Is(err, ErrInvalid) {
		t.Fatal("too many documents accepted")
	}
	team := fixture()[3]
	team.Documents = []WorkspaceDocument{doc}
	if err := team.Validate(); !errors.Is(err, ErrInvalid) {
		t.Fatal("documents accepted on a team")
	}
}
