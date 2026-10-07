package spannerstore

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"

	"ei-aitiger-codegraph/pkg/deployment"
)

// cursor is the signed page position. Scope fingerprints the database, the
// deployment scope and the query shape so a cursor from one listing is never
// accepted by another; Generation pins a listing to one graph generation.
type cursor struct {
	Scope      string `json:"s"`
	Generation uint64 `json:"g"`
	Position   string `json:"p"`
}

type cursorCodec struct {
	key      []byte
	domain   string
	maxBytes int
}

func fingerprint(v any) string {
	sum := sha256.Sum256(payload(v))
	return hex.EncodeToString(sum[:])
}

func (c cursorCodec) scope(parts ...any) string {
	return fingerprint(append([]any{c.domain}, parts...))
}

// encode signs a position. An empty position means "no next page".
func (c cursorCodec) encode(scope string, generation uint64, position string) string {
	if position == "" {
		return ""
	}
	b := payload(cursor{Scope: scope, Generation: generation, Position: position})
	h := hmac.New(sha256.New, c.key)
	h.Write(b)
	return base64.RawURLEncoding.EncodeToString(b) + "." + base64.RawURLEncoding.EncodeToString(h.Sum(nil))
}

// decode verifies a token for the given scope and generation and returns its
// position. An empty token is the first page.
func (c cursorCodec) decode(scope string, generation uint64, token string) (string, error) {
	if token == "" {
		return "", nil
	}
	if len(token) > c.maxBytes {
		return "", fmt.Errorf("%w: cursor too long", deployment.ErrLimitExceeded)
	}
	body, sig, ok := strings.Cut(token, ".")
	if !ok {
		return "", fmt.Errorf("%w: malformed cursor", deployment.ErrInvalidRequest)
	}
	b, err := base64.RawURLEncoding.DecodeString(body)
	if err != nil {
		return "", fmt.Errorf("%w: malformed cursor", deployment.ErrInvalidRequest)
	}
	signature, err := base64.RawURLEncoding.DecodeString(sig)
	if err != nil {
		return "", fmt.Errorf("%w: malformed cursor", deployment.ErrInvalidRequest)
	}
	h := hmac.New(sha256.New, c.key)
	h.Write(b)
	var cur cursor
	if !hmac.Equal(signature, h.Sum(nil)) || json.Unmarshal(b, &cur) != nil || cur.Scope != scope || cur.Position == "" {
		return "", fmt.Errorf("%w: cursor signature or scope", deployment.ErrInvalidRequest)
	}
	if cur.Generation != generation {
		return "", fmt.Errorf("%w: cursor generation %d, current %d", deployment.ErrStaleDeployment, cur.Generation, generation)
	}
	return cur.Position, nil
}
