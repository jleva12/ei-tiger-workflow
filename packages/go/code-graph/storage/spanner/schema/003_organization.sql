CREATE TABLE CGOrganizationEntities (
  TenantID STRING(128) NOT NULL,
  EntityID STRING(128) NOT NULL,
  Kind STRING(32) NOT NULL,
  PlatformID STRING(128) NOT NULL,
  ParentID STRING(128) NOT NULL,
  SlugKey STRING(256) NOT NULL,
  GraphRepositoryID STRING(256),
  Revision INT64 NOT NULL,
  Payload BYTES(MAX) NOT NULL
) PRIMARY KEY (TenantID, EntityID);
CREATE UNIQUE INDEX CGOrganizationSlugs ON CGOrganizationEntities(SlugKey);
CREATE UNIQUE NULL_FILTERED INDEX CGOrganizationGraphOwners ON CGOrganizationEntities(GraphRepositoryID);
