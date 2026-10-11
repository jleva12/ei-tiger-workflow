package spannerstore

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"

	"cloud.google.com/go/spanner"

	"ei-aitiger-codegraph/pkg/deployment"
)

func validSHA256(s string) bool {
	if len(s) != 64 || s != strings.ToLower(s) {
		return false
	}
	_, err := hex.DecodeString(s)
	return err == nil
}

// PutSource retains one source file by content hash. The row is written only
// when absent; existed reports whether it already was.
func (s *Store) PutSource(ctx context.Context, repo, sha256sum string, data []byte) (existed bool, err error) {
	if !validRepositoryID(repo) || !validSHA256(sha256sum) {
		return false, fmt.Errorf("%w: repository and content hash", deployment.ErrInvalidRequest)
	}
	if sum := sha256.Sum256(data); hex.EncodeToString(sum[:]) != sha256sum {
		return false, fmt.Errorf("%w: content does not match its hash", deployment.ErrInvalidRequest)
	}
	z, err := gzipBytes(data)
	if err != nil {
		return false, err
	}
	if int64(len(z)) > s.limits.MaxContentBytes {
		return false, fmt.Errorf("%w: compressed source %d bytes", deployment.ErrLimitExceeded, len(z))
	}
	err = s.tx(ctx, func(ctx context.Context, t *spanner.ReadWriteTransaction) error {
		existed = false
		_, err := t.ReadRow(ctx, "CGContent", spanner.Key{repo, sha256sum}, []string{"UncompressedBytes"})
		if err == nil {
			existed = true
			return nil
		}
		if !rowNotFound(err) {
			return mapRead(err, err)
		}
		return t.BufferWrite([]*spanner.Mutation{spanner.Insert("CGContent", []string{"RepositoryID", "SHA256", "UncompressedBytes", "Payload"}, []any{repo, sha256sum, int64(len(data)), z})})
	})
	return existed, err
}
