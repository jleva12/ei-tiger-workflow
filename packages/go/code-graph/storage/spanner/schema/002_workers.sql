CREATE TABLE CGWorkers (
 Queue STRING(128) NOT NULL,
 OwnerID STRING(256) NOT NULL,
 ConfigDigest STRING(71) NOT NULL,
 Languages ARRAY<STRING(64)> NOT NULL,
 BuildMode STRING(64) NOT NULL,
 MaxAttempts INT64 NOT NULL,
 Embeddings BOOL NOT NULL,
 StartedAt TIMESTAMP NOT NULL,
 HeartbeatAt TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp = true)
) PRIMARY KEY (Queue, OwnerID);
