CREATE TABLE IF NOT EXISTS casbin_rule (
 id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 ptype VARCHAR(8) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
 v0 VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
 v1 VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
 v2 VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
 v3 VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
 v4 VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
 v5 VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '',
 UNIQUE KEY unique_index (ptype,v0,v1,v2,v3,v4,v5)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS authz_roles (
 tenant_id VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 `key` VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 name VARCHAR(200) NOT NULL, description TEXT NOT NULL,
 created_at DATETIME(6) NOT NULL, updated_at DATETIME(6) NOT NULL,
 PRIMARY KEY (tenant_id,`key`)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS authz_permission_groups LIKE authz_roles;
CREATE TABLE IF NOT EXISTS authz_permission_catalog (
 resource_kind VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 action VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 description TEXT NOT NULL, conditions JSON NOT NULL, collection BOOLEAN NOT NULL,
 PRIMARY KEY (resource_kind,action)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS authz_external_groups (
 group_key VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
 issuer TEXT NOT NULL, corporate_tenant TEXT NOT NULL, external_id TEXT NOT NULL,
 display_name VARCHAR(200) NOT NULL, last_verified_at DATETIME(6) NOT NULL
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS authz_revision (
 id TINYINT NOT NULL PRIMARY KEY, revision BIGINT UNSIGNED NOT NULL,
 model_version VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL
) ENGINE=InnoDB;
INSERT IGNORE INTO authz_revision VALUES (1,1,'codegraph-authz-v1');
CREATE TABLE IF NOT EXISTS authz_audit_events (
 id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 actor VARCHAR(200) NOT NULL, tenant_id VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 request_id VARCHAR(100) NOT NULL, operation VARCHAR(100) NOT NULL,
 `before` JSON NOT NULL, `after` JSON NOT NULL, revision BIGINT UNSIGNED NOT NULL,
 created_at DATETIME(6) NOT NULL, INDEX audit_tenant_revision (tenant_id,revision)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS authz_outbox (
 id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
 revision BIGINT UNSIGNED NOT NULL, tenant_id VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 event_type VARCHAR(64) NOT NULL, published_at DATETIME(6) NULL,
 attempts INT NOT NULL DEFAULT 0, next_attempt_at DATETIME(6) NOT NULL,
 INDEX pending_events (published_at,next_attempt_at)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS authz_requests (
 tenant_id VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 actor VARCHAR(200) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 request_key VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 fingerprint VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 revision BIGINT UNSIGNED NOT NULL, PRIMARY KEY (tenant_id,actor,request_key)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS authz_bootstrap (
 tenant_id VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
 revision BIGINT UNSIGNED NOT NULL
) ENGINE=InnoDB;
