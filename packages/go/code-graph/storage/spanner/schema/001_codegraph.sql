CREATE TABLE CGRepositories (
 RepositoryID STRING(256) NOT NULL,
 LiveGeneration INT64 NOT NULL,
 LiveCommit STRING(64) NOT NULL,
 LiveRunID STRING(256) NOT NULL,
 LeaseOwner STRING(256) NOT NULL,
 LeaseRunID STRING(256) NOT NULL,
 LeaseToken INT64 NOT NULL,
 LeaseExpiresAt TIMESTAMP,
 Revision INT64 NOT NULL,
 Payload BYTES(MAX) NOT NULL
) PRIMARY KEY (RepositoryID);
CREATE TABLE CGRuns (
 RepositoryID STRING(256) NOT NULL,
 RunID STRING(256) NOT NULL,
 DeploymentID STRING(256) NOT NULL,
 DeploymentSequence INT64 NOT NULL,
 CommitSHA STRING(64) NOT NULL,
 ConfigDigest STRING(71) NOT NULL,
 IdentityDigest STRING(71) NOT NULL,
 Phase STRING(16) NOT NULL,
 Generation INT64 NOT NULL,
 Revision INT64 NOT NULL,
 AcceptedAt TIMESTAMP NOT NULL,
 UpdatedAt TIMESTAMP NOT NULL,
 Payload BYTES(MAX) NOT NULL
) PRIMARY KEY (RepositoryID, RunID);
CREATE UNIQUE INDEX CGRunsByIdentity ON CGRuns(RepositoryID, IdentityDigest);
CREATE INDEX CGRunsByPhase ON CGRuns(RepositoryID, Phase, DeploymentSequence);
CREATE TABLE CGSubmissions (
 SubmissionID STRING(256) NOT NULL,
 RepositoryID STRING(256) NOT NULL,
 RunID STRING(256) NOT NULL
) PRIMARY KEY (SubmissionID);
CREATE TABLE CGRecords (
 RepositoryID STRING(256) NOT NULL,
 RecordKind STRING(4) NOT NULL,
 RecordID STRING(256) NOT NULL,
 GenFrom INT64 NOT NULL,
 GenTo INT64,
 CommitFrom STRING(64) NOT NULL,
 CommitTo STRING(64) NOT NULL,
 Retired BOOL NOT NULL,
 Lineage STRING(256),
 Kind STRING(64) NOT NULL,
 Name STRING(MAX) NOT NULL,
 SourceID STRING(256),
 TargetID STRING(256),
 FactDigest STRING(71) NOT NULL,
 SearchHash STRING(64),
 Payload BYTES(MAX) NOT NULL
) PRIMARY KEY (RepositoryID, RecordKind, RecordID, GenFrom);
CREATE NULL_FILTERED INDEX CGRecordsByLineage ON CGRecords(RepositoryID, Lineage, GenFrom) STORING (GenTo, FactDigest);
CREATE INDEX CGRecordsByKind ON CGRecords(RepositoryID, RecordKind, Kind, RecordID, GenFrom) STORING (GenTo);
CREATE NULL_FILTERED INDEX CGEdgesBySource ON CGRecords(RepositoryID, SourceID, RecordID, GenFrom) STORING (GenTo, Kind, TargetID);
CREATE NULL_FILTERED INDEX CGEdgesByTarget ON CGRecords(RepositoryID, TargetID, RecordID, GenFrom) STORING (GenTo, Kind, SourceID);
CREATE NULL_FILTERED INDEX CGRecordsByGenTo ON CGRecords(RepositoryID, GenTo);
CREATE INDEX CGRecordsByGenFrom ON CGRecords(RepositoryID, GenFrom);
CREATE TABLE CGFileIdentities (
 RepositoryID STRING(256) NOT NULL,
 Lineage STRING(256) NOT NULL,
 Generation INT64 NOT NULL,
 Path STRING(MAX) NOT NULL,
 ContentSHA256 STRING(64) NOT NULL,
 Payload BYTES(MAX) NOT NULL
) PRIMARY KEY (RepositoryID, Lineage, Generation);
CREATE INDEX CGFileIdentitiesByGeneration ON CGFileIdentities(RepositoryID, Generation);
CREATE INDEX CGFileIdentitiesByPath ON CGFileIdentities(RepositoryID, Path, Lineage, Generation);
CREATE TABLE CGContent (
 RepositoryID STRING(256) NOT NULL,
 SHA256 STRING(64) NOT NULL,
 UncompressedBytes INT64 NOT NULL,
 Payload BYTES(MAX) NOT NULL
) PRIMARY KEY (RepositoryID, SHA256);
CREATE TABLE CGSearchEmbeddings (
 RepositoryID STRING(256) NOT NULL,
 Model STRING(256) NOT NULL,
 Dimensions INT64 NOT NULL,
 NodeID STRING(256) NOT NULL,
 DocumentHash STRING(64) NOT NULL,
 DocumentVersion INT64 NOT NULL,
 DocumentText STRING(MAX) NOT NULL,
 Embedding ARRAY<FLOAT32>(vector_length=>{{VECTOR_LENGTH}}) NOT NULL,
 UpdatedGeneration INT64 NOT NULL
) PRIMARY KEY (RepositoryID, Model, Dimensions, NodeID);
CREATE VECTOR INDEX CGSearchEmbeddingsVector ON CGSearchEmbeddings(Embedding) STORING (DocumentHash, DocumentVersion) OPTIONS (distance_type = 'COSINE');
CREATE TABLE CGGenerationInputs (
 RepositoryID STRING(256) NOT NULL,
 Generation INT64 NOT NULL,
 ContextID STRING(256) NOT NULL,
 ConfigDigest STRING(71) NOT NULL,
 Payload BYTES(MAX) NOT NULL
) PRIMARY KEY (RepositoryID, Generation);
CREATE TABLE CGSearchDocuments (
 RepositoryID STRING(256) NOT NULL,
 NodeID STRING(256) NOT NULL,
 Generation INT64 NOT NULL,
 Kind STRING(64) NOT NULL,
 Name STRING(MAX) NOT NULL,
 NameLower STRING(MAX) NOT NULL,
 QualifiedName STRING(MAX) NOT NULL,
 FilePath STRING(MAX) NOT NULL,
 DocumentHash STRING(64) NOT NULL,
 DocumentVersion INT64 NOT NULL,
 DocumentText STRING(MAX) NOT NULL,
 Identifiers STRING(MAX) NOT NULL,
 Tokens TOKENLIST AS (TOKENIZE_FULLTEXT(DocumentText)) HIDDEN,
 IdentifierTokens TOKENLIST AS (TOKENIZE_FULLTEXT(Identifiers)) HIDDEN,
 IdentifierSubstrings TOKENLIST AS (TOKENIZE_SUBSTRING(Identifiers)) HIDDEN
) PRIMARY KEY (RepositoryID, NodeID);
CREATE INDEX CGSearchDocumentsByName ON CGSearchDocuments(RepositoryID, NameLower, NodeID) STORING (Kind, QualifiedName, DocumentHash);
CREATE SEARCH INDEX CGSearchDocumentsIndex ON CGSearchDocuments(Tokens, IdentifierTokens, IdentifierSubstrings) STORING (Kind, FilePath, DocumentHash, DocumentVersion) PARTITION BY RepositoryID;
