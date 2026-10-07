CREATE TABLE IF NOT EXISTS authz_user_integrations (
 tenant_id VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 subject_id VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 provider VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 account_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 account_login VARCHAR(100) NOT NULL,
 status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 credential VARBINARY(8192) NOT NULL,
 revision BIGINT NOT NULL,
 connected_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL,
 PRIMARY KEY (tenant_id, subject_id, provider)
) ENGINE=InnoDB;
