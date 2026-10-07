package authorization

import (
	"errors"
	"slices"

	"gorm.io/gorm"
)

// Upgrade the opt-in local fixture once. Do not re-create removed roles,
// mappings, or grants, and do not restore a subsequently revoked site grant.
func upgradeLocalSiteConfiguration(tx *gorm.DB, identity Identity, current uint64) (uint64, error) {
	const marker = "local-site-configuration-v3"
	var prior requestRow
	err := tx.Table("authz_requests").Where("tenant_id=? AND request_key=?", identity.TenantID, marker).Take(&prior).Error
	if err == nil {
		return current, nil
	}
	if !errors.Is(err, gorm.ErrRecordNotFound) {
		return 0, err
	}
	before, err := readConfiguration(tx, identity.TenantID)
	if err != nil {
		return 0, err
	}
	grants := []Grant{}
	for _, r := range before.Rules {
		if r.Type == "p" && len(r.Values) == 5 && r.Values[0] == localSiteAdminPermissions {
			grants = append(grants, Grant{r.Values[2], r.Values[3], r.Values[4]})
		}
	}
	updated := append([]Grant(nil), grants...)
	for _, extension := range []struct{ existing, added Grant }{
		{Grant{"organization", "read", "tenant"}, Grant{"site_configuration", "read", "tenant"}},
		{Grant{"organization_collection", "create", "tenant"}, Grant{"site_configuration", "write", "tenant"}},
		{Grant{"organization", "read", "tenant"}, Grant{"organization", "enter", "tenant"}},
		{Grant{"organization", "read", "tenant"}, Grant{"organization", "manage_members", "tenant"}},
		{Grant{"organization", "read", "tenant"}, Grant{"team_collection", "enter", "tenant"}},
		{Grant{"organization", "read", "tenant"}, Grant{"user_directory", "read", "tenant"}},
		{Grant{"organization_collection", "create", "tenant"}, Grant{"user_directory", "invite", "tenant"}},
	} {
		if slices.Contains(grants, extension.existing) && !slices.Contains(updated, extension.added) {
			updated = append(updated, extension.added)
		}
	}
	committed := current
	if len(updated) != len(grants) {
		if err := applyCommand(tx, identity.TenantID, Command{Operation: "group.grants", Key: localSiteAdminPermissions, Grants: updated}); err != nil {
			return 0, err
		}
		after, err := readConfiguration(tx, identity.TenantID)
		if err != nil {
			return 0, err
		}
		if err := validateConfiguration(after, true); err != nil {
			return 0, err
		}
		committed++
		after.Revision = committed
		if err := auditCommit(tx, identity.SubjectID, identity.TenantID, "", "local_site_configuration_upgrade", before, after, committed); err != nil {
			return 0, err
		}
	}
	if err := tx.Table("authz_requests").Create(&requestRow{identity.TenantID, identity.SubjectID, marker, stableKey("", marker), committed}).Error; err != nil {
		return 0, err
	}
	return committed, nil
}
