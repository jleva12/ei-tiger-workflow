-- Shared by the Python writers and Go MCP readers. Separate from CG* graph tables.
CREATE TABLE ForgeVectorRecords (
  Namespace STRING(32) NOT NULL,
  TenantId STRING(256) NOT NULL,
  Id STRING(256) NOT NULL,
  Payload JSON NOT NULL
) PRIMARY KEY (Namespace, TenantId, Id);
CREATE TABLE ForgeVectors (
  Namespace STRING(32) NOT NULL,
  TenantId STRING(256) NOT NULL,
  ScopeId STRING(256) NOT NULL,
  Id STRING(256) NOT NULL,
  Payload JSON NOT NULL,
  RunSeq INT64 NOT NULL,
  ContentHash STRING(256) NOT NULL,
  EmbeddingModel STRING(256),
  Embedding ARRAY<FLOAT64>,
  SearchText STRING(MAX) NOT NULL,
  Title STRING(MAX) NOT NULL,
  SectionText STRING(MAX) NOT NULL,
  ContextText STRING(MAX) NOT NULL,
  Identifiers ARRAY<STRING(MAX)>,
  TextTokens TOKENLIST AS (TOKENIZE_FULLTEXT(SearchText)) HIDDEN,
  TitleTokens TOKENLIST AS (TOKENIZE_FULLTEXT(Title)) HIDDEN,
  SectionTokens TOKENLIST AS (TOKENIZE_FULLTEXT(SectionText)) HIDDEN,
  ContextTokens TOKENLIST AS (TOKENIZE_FULLTEXT(ContextText)) HIDDEN
) PRIMARY KEY (Namespace, TenantId, ScopeId, Id);
CREATE INDEX ForgeVectorsByHash ON ForgeVectors(Namespace, TenantId, ContentHash);
CREATE INDEX ForgeVectorsByScope ON ForgeVectors(Namespace, ScopeId, Id);
CREATE SEARCH INDEX ForgeVectorsText ON ForgeVectors(TextTokens, TitleTokens, SectionTokens, ContextTokens)
  -- MCP searches several authorized tenants in one query. Spanner requires
  -- one search-index partition per query; tenant predicates remain on every leg.
  PARTITION BY Namespace;
