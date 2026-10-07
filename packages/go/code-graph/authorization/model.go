package authorization

import (
	"fmt"
	"slices"
	"strings"

	"github.com/casbin/casbin/v3"
	"github.com/casbin/casbin/v3/model"
	"github.com/casbin/casbin/v3/persist"
)

const ModelVersion = "codegraph-authz-v1"
const Model = `[request_definition]
r = sub, dom, obj, act
[policy_definition]
p = sub, dom, obj, act, scope
[role_definition]
g = _, _, _
[policy_effect]
e = some(where (p.eft == allow))
[matchers]
m = r.sub.TenantID == r.dom && r.obj.TenantID == r.dom && r.dom == p.dom && hasRole(r.sub, p.sub) && r.obj.Kind == p.obj && r.act == p.act && scopeMatch(p.scope, r.sub, r.obj)
`

// One registry drives in-memory evaluation and parameterized query predicates.
type condition struct {
	owner  bool
	status string
}

var conditions = map[string]condition{
	"tenant": {}, "owner": {owner: true}, "owner_draft": {owner: true, status: "draft"},
	"tenant_draft": {status: "draft"}, "tenant_submitted": {status: "submitted"},
}

func matches(name string, p Principal, r Resource) bool {
	c, ok := conditions[name]
	return ok && p.SubjectID != "" && p.TenantID != "" && p.TenantID == r.TenantID &&
		(!c.owner || r.OwnerID != "" && r.OwnerID == p.SubjectID) && (c.status == "" || r.Status == c.status)
}
func enforcer(adapter persist.Adapter) (*casbin.SyncedEnforcer, error) {
	m, err := model.NewModelFromString(Model)
	if err != nil {
		return nil, err
	}
	e, err := casbin.NewSyncedEnforcer(m)
	if err != nil {
		return nil, err
	}
	e.EnableAutoSave(false)
	e.AddFunction("hasRole", func(args ...interface{}) (interface{}, error) {
		if len(args) != 2 {
			return false, ErrUnavailable
		}
		p, ok := args[0].(Principal)
		key, ok2 := args[1].(string)
		if !ok || !ok2 {
			return false, ErrUnavailable
		}
		return slices.Contains(p.RoleIDs, key), nil
	})
	e.AddFunction("scopeMatch", func(args ...interface{}) (interface{}, error) {
		if len(args) != 3 {
			return false, ErrUnavailable
		}
		name, a := args[0].(string)
		p, b := args[1].(Principal)
		r, c := args[2].(Resource)
		if !a || !b || !c {
			return false, ErrUnavailable
		}
		return matches(name, p, r), nil
	})
	if adapter != nil {
		e.SetAdapter(adapter)
		if err = e.LoadPolicy(); err != nil {
			return nil, err
		}
	}
	return e, nil
}

// membershipAllows is the application's own membership rule, evaluated beside
// policy grants. Members read and enter their team; leads manage its roster;
// members of every owning team read and edit team-owned records. Creation is
// decided separately because a new record has no persisted owners yet.
func membershipAllows(p Principal, r Resource, action string) bool {
	if r.Kind != "organization" || r.TenantID == "" || len(p.Memberships) == 0 {
		return false
	}
	if r.Team != "" {
		role, member := p.Memberships[membershipKey(r.TenantID, r.Team)]
		switch action {
		case "read", "enter":
			return member
		case "manage_members":
			return member && role == LeadRole
		}
		return false
	}
	if len(r.Teams) == 0 {
		return false
	}
	for _, team := range r.Teams {
		if _, member := p.Memberships[membershipKey(r.TenantID, team)]; !member {
			return false
		}
	}
	switch action {
	case "read", "update", "archive", "transition":
		return true
	}
	return false
}

type Scope struct {
	TenantID, SubjectID string
	Templates           []string
	SiteConfiguration   bool
}

func (s Scope) Allows(r Resource) bool {
	if s.SiteConfiguration && r.Kind == "organization" && r.TenantID != "" {
		return true
	}
	for _, t := range s.Templates {
		if matches(t, Principal{SubjectID: s.SubjectID, TenantID: s.TenantID}, r) {
			return true
		}
	}
	return false
}

// Columns are trusted repository SQL expressions, never request or policy text.
type Columns struct{ Tenant, Owner, Status string }

func (s Scope) SQL(c Columns) (string, map[string]any, error) {
	args := map[string]any{"authzTenant": s.TenantID}
	if c.Tenant == "" || s.TenantID == "" || s.SubjectID == "" {
		return "FALSE", args, ErrUnavailable
	}
	if s.SiteConfiguration {
		return c.Tenant + " != ''", map[string]any{}, nil
	}
	parts := []string{}
	for _, name := range s.Templates {
		condition, ok := conditions[name]
		if !ok {
			return "FALSE", args, ErrUnavailable
		}
		terms := []string{}
		if condition.owner {
			if c.Owner == "" {
				return "FALSE", args, ErrUnavailable
			}
			args["authzSubject"] = s.SubjectID
			terms = append(terms, c.Owner+" = @authzSubject")
		}
		if condition.status != "" {
			if c.Status == "" {
				return "FALSE", args, ErrUnavailable
			}
			key := fmt.Sprintf("authzStatus%d", len(parts))
			args[key] = condition.status
			terms = append(terms, c.Status+" = @"+key)
		}
		if len(terms) == 0 {
			terms = append(terms, "TRUE")
		}
		parts = append(parts, "("+strings.Join(terms, " AND ")+")")
	}
	if len(parts) == 0 {
		parts = append(parts, "FALSE")
	}
	return c.Tenant + " = @authzTenant AND (" + strings.Join(parts, " OR ") + ")", args, nil
}
