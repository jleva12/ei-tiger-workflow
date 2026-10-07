package organization

import (
	"fmt"
	"slices"
)

type ScopeRequest struct {
	TenantID   string   `json:"tenant_id"`
	PlatformID string   `json:"platform_id"`
	Mode       string   `json:"mode" enum:"selected_teams,entire_platform"`
	TeamIDs    []string `json:"team_ids" maxItems:"200"`
}

type Contributor struct {
	TeamID           string   `json:"team_id"`
	TeamName         string   `json:"team_name"`
	ResponsibilityID string   `json:"responsibility_id"`
	Name             string   `json:"name"`
	ScopeKind        string   `json:"scope_kind"`
	Paths            []string `json:"paths"`
}

type RepositoryScope struct {
	RepositoryID      string        `json:"repository_id"`
	GraphRepositoryID string        `json:"graph_repository_id"`
	Name              string        `json:"name"`
	RemoteURL         string        `json:"remote_url"`
	ScopeKind         string        `json:"scope_kind"`
	Paths             []string      `json:"paths"`
	Contributors      []Contributor `json:"contributors"`
}

type Scope struct {
	Repositories []RepositoryScope `json:"repositories"`
}

// Resolve deduplicates code contributed by the selected teams. Empty selections
// stay empty. File paths are context filters, not authorization boundaries.
func Resolve(records []Entity, request ScopeRequest) (Scope, error) {
	out := Scope{Repositories: []RepositoryScope{}}
	tenant, ok := Find(records, request.TenantID)
	if !ok || tenant.Kind != Tenant || tenant.Status != Active {
		return out, fmt.Errorf("%w: active tenant is required", ErrInvalid)
	}
	platform, ok := Find(records, request.PlatformID)
	if !ok || platform.Kind != Platform || platform.TenantID != request.TenantID || platform.Status != Active {
		return out, fmt.Errorf("%w: active platform must belong to the tenant", ErrInvalid)
	}
	if request.Mode != SelectedTeams && request.Mode != EntirePlatform || len(request.TeamIDs) > 200 || request.Mode == EntirePlatform && len(request.TeamIDs) > 0 {
		return out, fmt.Errorf("%w: invalid workspace scope", ErrInvalid)
	}
	selected := map[string]bool{}
	for _, id := range request.TeamIDs {
		t, ok := Find(records, id)
		if !ok || t.Kind != Team || t.Status != Active || t.TenantID != request.TenantID || t.PlatformID != request.PlatformID || selected[id] {
			return out, fmt.Errorf("%w: selected teams must be active and belong to the same platform", ErrInvalid)
		}
		selected[id] = true
	}
	for _, repo := range records {
		if repo.Kind != Repository || repo.Status != Active || repo.TenantID != request.TenantID || repo.PlatformID != request.PlatformID {
			continue
		}
		entry := RepositoryScope{RepositoryID: repo.ID, GraphRepositoryID: repo.GraphRepositoryID, Name: repo.Name, RemoteURL: repo.RemoteURL, ScopeKind: Paths, Paths: []string{}, Contributors: []Contributor{}}
		if request.Mode == EntirePlatform {
			entry.ScopeKind = WholeRepository
		} else {
			for _, a := range records {
				if a.Kind != Responsibility || a.Status != Active || a.TenantID != request.TenantID || a.PlatformID != request.PlatformID || a.RepositoryID != repo.ID || !selected[a.ParentID] {
					continue
				}
				t, _ := Find(records, a.ParentID)
				entry.Contributors = append(entry.Contributors, Contributor{t.ID, t.Name, a.ID, a.Name, a.ScopeKind, slices.Clone(a.Paths)})
				if a.ScopeKind == WholeRepository {
					entry.ScopeKind = WholeRepository
				} else {
					entry.Paths = append(entry.Paths, a.Paths...)
				}
			}
			if len(entry.Contributors) == 0 {
				continue
			}
		}
		if entry.ScopeKind == WholeRepository {
			entry.Paths = []string{}
		} else {
			entry.Paths = canonicalPaths(entry.Paths)
		}
		slices.SortFunc(entry.Contributors, func(a, b Contributor) int { return compare(a.TeamName+a.Name, b.TeamName+b.Name) })
		out.Repositories = append(out.Repositories, entry)
	}
	slices.SortFunc(out.Repositories, func(a, b RepositoryScope) int { return compare(a.Name+a.RepositoryID, b.Name+b.RepositoryID) })
	return out, nil
}

func compare(a, b string) int {
	if a < b {
		return -1
	}
	if a > b {
		return 1
	}
	return 0
}
