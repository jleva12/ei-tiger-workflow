CREATE TABLE IF NOT EXISTS authz_users (
 subject_id VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
 tenant_id VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 issuer TEXT NOT NULL, external_id TEXT NOT NULL,
 display_name VARCHAR(200) NOT NULL, email VARCHAR(320) NOT NULL,
 status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 source VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 first_seen_at DATETIME(6) NOT NULL, last_seen_at DATETIME(6) NOT NULL,
 INDEX users_tenant_name (tenant_id, display_name, subject_id)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS authz_team_members (
 tenant_id VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 team_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 subject_id VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 role VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 added_by VARCHAR(200) NOT NULL, added_at DATETIME(6) NOT NULL,
 PRIMARY KEY (tenant_id, team_id, subject_id),
 INDEX members_subject (subject_id)
) ENGINE=InnoDB;
