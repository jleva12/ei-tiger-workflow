CREATE TABLE CGCrossLinks (
 Owner STRING(128) NOT NULL,
 LinkID STRING(128) NOT NULL,
 Kind STRING(64) NOT NULL,
 SourceRepositoryID STRING(256) NOT NULL,
 SourceNodeID STRING(256) NOT NULL,
 SourceQualifiedName STRING(MAX),
 TargetRepositoryID STRING(256) NOT NULL,
 TargetNodeID STRING(256) NOT NULL,
 TargetQualifiedName STRING(MAX),
 Payload BYTES(MAX) NOT NULL,
 UpdatedAt TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp = true)
) PRIMARY KEY (Owner, LinkID);
CREATE INDEX CGCrossLinksBySource ON CGCrossLinks(SourceRepositoryID, SourceNodeID);
CREATE INDEX CGCrossLinksByTarget ON CGCrossLinks(TargetRepositoryID, TargetNodeID);
