package graph

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"strings"
)

// ID derives a compact, collision-safe identifier from a kind and ordered
// parts: kind ":" base64url of the first 16 bytes of SHA-256(JSON(parts)).
// 128 bits is far beyond any realistic key space and a third the size of a
// hex SHA-256, which matters because every record carries several IDs.
func ID(kind string, parts ...string) string {
	b, _ := json.Marshal(parts)
	sum := sha256.Sum256(b)
	return kind + ":" + base64.RawURLEncoding.EncodeToString(sum[:16])
}

// Lineage identifies a file's compilation identity independently of commit:
// repository, module, source set and path. A rename detected by git may
// continue an old lineage; see the ingestion pipeline.
func Lineage(repositoryID, moduleID, sourceSetID, path string) string {
	return ID("file", repositoryID, moduleID, sourceSetID, path)
}

// Digest is the canonical content hash of any JSON-serializable value,
// formatted as sha256:<hex>.
func Digest(v any) string {
	b, _ := json.Marshal(v)
	sum := sha256.Sum256(b)
	return "sha256:" + hex.EncodeToString(sum[:])
}

// Digest of a fact covers its canonical JSON, so location and property
// changes are observable while stable facts compare equal across runs.
func (f Fact) Digest() string { return Digest(f) }

// ValidID reports whether an identifier has the kind:token shape and a
// bounded length. Kinds are lowercase words; tokens are base64url.
func ValidID(id string) bool {
	if id == "" || len(id) > 256 {
		return false
	}
	i := strings.IndexByte(id, ':')
	if i <= 0 || i == len(id)-1 {
		return false
	}
	for _, c := range id[:i] {
		if !(c >= 'a' && c <= 'z' || c == '_') {
			return false
		}
	}
	for _, c := range id[i+1:] {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '-' || c == '_') {
			return false
		}
	}
	return true
}
