// Package organization defines the configuration of the AI development platform.
// Ownership scopes describe working context; they do not grant access to code.
package organization

import (
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strings"
	"time"
)

type Kind string

const (
	Tenant          Kind = "tenant"
	Platform        Kind = "platform"
	Team            Kind = "team"
	Repository      Kind = "repository"
	Responsibility  Kind = "responsibility"
	Workspace       Kind = "workspace"
	Active               = "active"
	Draft                = "draft"
	Archived             = "archived"
	WholeRepository      = "whole_repository"
	Paths                = "paths"
	SelectedTeams        = "selected_teams"
	EntirePlatform       = "entire_platform"
)

var (
	ErrInvalid  = errors.New("invalid organization configuration")
	ErrConflict = errors.New("organization configuration conflict")
	ErrNotFound = errors.New("organization record not found")
	identifier  = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)
	slugPattern = regexp.MustCompile(`^[a-z0-9]+(?:-[a-z0-9]+)*$`)
)

// Entity is a tenant-qualified configuration record. Kind-specific references
// are validated before persistence; parents and kind are immutable after create.
type Entity struct {
	OwnerID           string              `json:"owner_id,omitempty"`
	ID                string              `json:"id"`
	TenantID          string              `json:"tenant_id"`
	PlatformID        string              `json:"platform_id,omitempty"`
	ParentID          string              `json:"parent_id,omitempty"`
	Kind              Kind                `json:"kind" enum:"tenant,platform,team,repository,responsibility,workspace"`
	Name              string              `json:"name" maxLength:"100"`
	Slug              string              `json:"slug" maxLength:"64"`
	Description       string              `json:"description,omitempty" maxLength:"2000"`
	Status            string              `json:"status" enum:"active,draft,archived"`
	GraphRepositoryID string              `json:"graph_repository_id,omitempty"`
	RemoteURL         string              `json:"remote_url,omitempty"`
	RepositoryID      string              `json:"repository_id,omitempty"`
	ScopeKind         string              `json:"scope_kind,omitempty"`
	Paths             []string            `json:"paths,omitempty" maxItems:"100"`
	ScopeMode         string              `json:"scope_mode,omitempty"`
	TeamIDs           []string            `json:"team_ids,omitempty" maxItems:"200"`
	Documents         []WorkspaceDocument `json:"documents,omitempty" maxItems:"50"`
	Revision          int64               `json:"revision" minimum:"0"`
	CreatedAt         time.Time           `json:"created_at,omitempty"`
	UpdatedAt         time.Time           `json:"updated_at,omitempty"`
}

func ValidID(id string) bool { return identifier.MatchString(id) }

func (e Entity) Validate() error {
	if !ValidID(e.ID) || !ValidID(e.TenantID) || (e.Revision < 0 || e.Revision >= 9007199254740991) {
		return fmt.Errorf("%w: invalid identity or revision", ErrInvalid)
	}
	if strings.TrimSpace(e.Name) != e.Name || e.Name == "" || len(e.Name) > 100 || len(e.Description) > 2000 {
		return fmt.Errorf("%w: enter a name of up to 100 characters and description of up to 2000 characters", ErrInvalid)
	}
	if len(e.Slug) > 64 || !slugPattern.MatchString(e.Slug) {
		return fmt.Errorf("%w: use a lowercase slug with letters, numbers and single hyphens", ErrInvalid)
	}
	if e.Status != Active && e.Status != Archived && !(e.Kind == Workspace && e.Status == Draft) {
		return fmt.Errorf("%w: invalid lifecycle status", ErrInvalid)
	}
	if e.Kind != Repository && (e.GraphRepositoryID != "" || e.RemoteURL != "") || e.Kind != Responsibility && (e.RepositoryID != "" || e.ScopeKind != "" || len(e.Paths) != 0) || e.Kind != Workspace && (e.ScopeMode != "" || len(e.TeamIDs) != 0 || len(e.Documents) != 0) {
		return fmt.Errorf("%w: fields do not match record kind", ErrInvalid)
	}
	switch e.Kind {
	case Tenant:
		if e.TenantID != e.ID || e.ParentID != "" || e.PlatformID != "" {
			return fmt.Errorf("%w: tenant cannot have a parent", ErrInvalid)
		}
	case Platform:
		if e.ParentID != e.TenantID || e.PlatformID != e.ID {
			return fmt.Errorf("%w: platform must belong to its tenant", ErrInvalid)
		}
	case Team, Repository, Workspace, Responsibility:
		if !ValidID(e.PlatformID) || !ValidID(e.ParentID) || e.ID == e.ParentID {
			return fmt.Errorf("%w: platform and parent are required", ErrInvalid)
		}
		if e.Kind != Responsibility && e.ParentID != e.PlatformID {
			return fmt.Errorf("%w: record must belong to its platform", ErrInvalid)
		}
	default:
		return fmt.Errorf("%w: unknown record kind", ErrInvalid)
	}
	if e.Kind == Repository && (e.GraphRepositoryID == "" || len(e.GraphRepositoryID) > 256 || !strings.HasPrefix(e.RemoteURL, "https://github.com/")) {
		return fmt.Errorf("%w: select a registered GitHub repository", ErrInvalid)
	}
	if e.Kind == Responsibility {
		if !ValidID(e.RepositoryID) {
			return fmt.Errorf("%w: repository is required", ErrInvalid)
		}
		if e.ScopeKind == WholeRepository {
			if len(e.Paths) > 0 {
				return fmt.Errorf("%w: whole repository scope must not contain paths", ErrInvalid)
			}
		} else if e.ScopeKind == Paths {
			if len(e.Paths) == 0 || len(e.Paths) > 100 {
				return fmt.Errorf("%w: provide between 1 and 100 paths", ErrInvalid)
			}
			seen := map[string]bool{}
			for _, p := range e.Paths {
				if err := ValidatePath(p); err != nil {
					return err
				}
				if seen[p] {
					return fmt.Errorf("%w: duplicate path %q", ErrInvalid, p)
				}
				seen[p] = true
			}
		} else {
			return fmt.Errorf("%w: choose whole repository or specific paths", ErrInvalid)
		}
	}
	if e.Kind == Workspace {
		if err := validateDocuments(e.Documents); err != nil {
			return err
		}
		if e.ScopeMode != SelectedTeams && e.ScopeMode != EntirePlatform {
			return fmt.Errorf("%w: choose a workspace scope mode", ErrInvalid)
		}
		if len(e.TeamIDs) > 200 || e.ScopeMode == EntirePlatform && len(e.TeamIDs) != 0 {
			return fmt.Errorf("%w: invalid workspace team selection", ErrInvalid)
		}
		seen := map[string]bool{}
		for _, id := range e.TeamIDs {
			if !ValidID(id) || seen[id] {
				return fmt.Errorf("%w: invalid or duplicate selected team", ErrInvalid)
			}
			seen[id] = true
		}
	}
	return nil
}

// A trailing slash represents a directory; other paths represent literal files.
func ValidatePath(p string) error {
	if p == "" || len(p) > 1024 || strings.TrimSpace(p) != p || strings.HasPrefix(p, "/") || strings.ContainsAny(p, "\\\x00\r\n*?[]:") {
		return fmt.Errorf("%w: %q must be a literal repository-relative file or directory", ErrInvalid, p)
	}
	for _, part := range strings.Split(strings.TrimSuffix(p, "/"), "/") {
		if part == "" || part == "." || part == ".." {
			return fmt.Errorf("%w: %q contains an invalid path segment", ErrInvalid, p)
		}
	}
	return nil
}

func Find(records []Entity, id string) (Entity, bool) {
	for _, e := range records {
		if e.ID == id {
			return e, true
		}
	}
	return Entity{}, false
}

func covers(a, b string) bool { return a == b || strings.HasSuffix(a, "/") && strings.HasPrefix(b, a) }

func canonicalPaths(paths []string) []string {
	paths = slices.Clone(paths)
	slices.Sort(paths)
	out := make([]string, 0, len(paths))
	for _, p := range paths {
		covered := false
		for _, existing := range out {
			if covers(existing, p) {
				covered = true
				break
			}
		}
		if !covered {
			out = append(out, p)
		}
	}
	return out
}

type Overlap struct {
	ResponsibilityID string `json:"responsibility_id"`
	TeamName         string `json:"team_name"`
	Name             string `json:"name"`
}

func Overlaps(records []Entity, candidate Entity) []Overlap {
	out := []Overlap{}
	if candidate.Kind != Responsibility {
		return out
	}
	for _, e := range records {
		if e.Kind != Responsibility || e.ID == candidate.ID || e.Status != Active || e.TenantID != candidate.TenantID || e.PlatformID != candidate.PlatformID || e.RepositoryID != candidate.RepositoryID {
			continue
		}
		overlap := e.ScopeKind == WholeRepository || candidate.ScopeKind == WholeRepository
		for _, a := range candidate.Paths {
			for _, b := range e.Paths {
				if covers(a, b) || covers(b, a) {
					overlap = true
				}
			}
		}
		if overlap {
			t, _ := Find(records, e.ParentID)
			out = append(out, Overlap{e.ID, t.Name, e.Name})
		}
	}
	return out
}

// Dependencies lists active records that need attention before archive.
func Dependencies(records []Entity, target Entity) []Entity {
	out := []Entity{}
	for _, e := range records {
		if e.ID == target.ID || e.TenantID != target.TenantID || e.Status == Archived {
			continue
		}
		dependent := e.ParentID == target.ID
		if target.Kind == Tenant {
			dependent = true
		}
		if target.Kind == Platform && e.PlatformID == target.ID {
			dependent = true
		}
		if target.Kind == Repository && e.Kind == Responsibility && e.RepositoryID == target.ID {
			dependent = true
		}
		if e.Kind == Workspace && e.Status == Active && e.PlatformID == target.PlatformID {
			if target.Kind == Team && e.ScopeMode == SelectedTeams && slices.Contains(e.TeamIDs, target.ID) {
				dependent = true
			}
			if target.Kind == Responsibility && e.ScopeMode == SelectedTeams && slices.Contains(e.TeamIDs, target.ParentID) {
				dependent = true
			}
			if target.Kind == Repository && e.ScopeMode == EntirePlatform {
				dependent = true
			}
		}
		if dependent {
			out = append(out, e)
		}
	}
	return out
}

// Prepare validates against a consistent tenant snapshot and stamps one write.
func Prepare(records []Entity, e Entity, acknowledgeOverlap bool, now time.Time) (Entity, error) {
	if err := e.Validate(); err != nil {
		return Entity{}, err
	}
	old, exists := Find(records, e.ID)
	if exists {
		if old.Revision != e.Revision {
			return Entity{}, fmt.Errorf("%w: this record changed; reload and review your edits", ErrConflict)
		}
		if old.TenantID != e.TenantID || old.PlatformID != e.PlatformID || old.ParentID != e.ParentID || old.Kind != e.Kind {
			return Entity{}, fmt.Errorf("%w: record parents and kind cannot change", ErrInvalid)
		}
		if old.OwnerID != e.OwnerID {
			return Entity{}, fmt.Errorf("%w: record owner cannot change", ErrInvalid)
		}
		if old.Status == Archived {
			return Entity{}, fmt.Errorf("%w: archived records cannot be edited", ErrConflict)
		}
		if e.Kind == Repository && (old.GraphRepositoryID != e.GraphRepositoryID || old.RemoteURL != e.RemoteURL) {
			return Entity{}, fmt.Errorf("%w: a repository's graph registration cannot change", ErrInvalid)
		}
		e.CreatedAt = old.CreatedAt
	} else {
		if e.Revision != 0 {
			return Entity{}, ErrNotFound
		}
		if e.Status == Archived {
			return Entity{}, fmt.Errorf("%w: create an active record first", ErrInvalid)
		}
		e.CreatedAt = now
	}
	if e.Kind != Tenant {
		tenant, ok := Find(records, e.TenantID)
		if !ok || tenant.Kind != Tenant || tenant.Status != Active {
			return Entity{}, fmt.Errorf("%w: active tenant is required", ErrInvalid)
		}
		parent, ok := Find(records, e.ParentID)
		want := Platform
		if e.Kind == Platform {
			want = Tenant
		}
		if e.Kind == Responsibility {
			want = Team
		}
		if !ok || parent.Kind != want || parent.Status != Active || parent.TenantID != e.TenantID || want != Tenant && parent.PlatformID != e.PlatformID {
			return Entity{}, fmt.Errorf("%w: parent must be active and belong to the same tenant and platform", ErrInvalid)
		}
	}
	for _, other := range records {
		if other.ID == e.ID {
			continue
		}
		if other.Kind == e.Kind && other.ParentID == e.ParentID && other.Slug == e.Slug {
			return Entity{}, fmt.Errorf("%w: this slug is already used in the selected parent", ErrConflict)
		}
		if e.Kind == Repository && other.Kind == Repository && other.PlatformID == e.PlatformID && other.RemoteURL == e.RemoteURL {
			return Entity{}, fmt.Errorf("%w: repository is already registered in this platform", ErrConflict)
		}
		if e.Kind == Responsibility && other.Kind == Responsibility && other.Status == Active && other.ParentID == e.ParentID && other.RepositoryID == e.RepositoryID && other.ScopeKind == e.ScopeKind && slices.Equal(canonicalPaths(other.Paths), canonicalPaths(e.Paths)) {
			return Entity{}, fmt.Errorf("%w: this team already has the same repository scope", ErrConflict)
		}
	}
	if e.Status == Archived {
		if dependencies := Dependencies(records, e); len(dependencies) > 0 {
			names := []string{}
			for _, d := range dependencies {
				names = append(names, d.Name)
			}
			return Entity{}, fmt.Errorf("%w: resolve dependent records before archiving: %s", ErrConflict, strings.Join(names, ", "))
		}
	} else if e.Kind == Responsibility {
		repo, ok := Find(records, e.RepositoryID)
		if !ok || repo.Kind != Repository || repo.Status != Active || repo.TenantID != e.TenantID || repo.PlatformID != e.PlatformID {
			return Entity{}, fmt.Errorf("%w: repository must belong to the team's platform", ErrInvalid)
		}
		if !acknowledgeOverlap && len(Overlaps(records, e)) > 0 {
			return Entity{}, fmt.Errorf("%w: acknowledge overlapping responsibility scopes before saving", ErrConflict)
		}
	} else if e.Kind == Workspace {
		_, err := Resolve(records, ScopeRequest{TenantID: e.TenantID, PlatformID: e.PlatformID, Mode: e.ScopeMode, TeamIDs: e.TeamIDs})
		if err != nil {
			return Entity{}, err
		}
	}
	if e.Kind == Workspace {
		e.Documents = stampDocuments(e.Documents, old.Documents, now)
	}
	e.Paths = canonicalPaths(e.Paths)
	e.TeamIDs = slices.Clone(e.TeamIDs)
	slices.Sort(e.TeamIDs)
	e.Revision++
	e.UpdatedAt = now
	return e, nil
}
