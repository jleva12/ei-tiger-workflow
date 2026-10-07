package authorization

import "context"

func siteConfigurationAction(kind, action string) string {
	switch kind + ":" + action {
	case "organization:read", "organization_collection:list", "team_collection:enter", "user_directory:read":
		return "read"
	case "organization:update", "organization:archive", "organization:transition", "organization:manage_members", "organization_collection:create", "user_directory:invite":
		return "write"
	}
	return ""
}

// CanAdministerSite requires both provider verification and an explicit live
// policy grant. A role name, request parameter, or tenant-admin grant alone
// cannot expand the organization boundary. One deployment represents one site.
func CanAdministerSite(ctx context.Context, action string) (bool, error) {
	i, err := CurrentIdentity(ctx)
	if err != nil {
		return false, err
	}
	d, err := Check(ctx, Resource{Kind: "site_configuration", TenantID: i.TenantID}, action)
	return d.Allowed, err
}
