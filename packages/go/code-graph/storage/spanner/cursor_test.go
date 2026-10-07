package spannerstore

import (
	"errors"
	"strings"
	"testing"

	"ei-aitiger-codegraph/pkg/deployment"
)

func testCodec() cursorCodec {
	return cursorCodec{key: []byte("0123456789abcdef0123456789abcdef"), domain: "projects/p/instances/i/databases/d|test", maxBytes: 8 << 10}
}

func TestCursorRoundTrip(t *testing.T) {
	c := testCodec()
	scope := c.scope("list", "repo", "node", "class")
	token := c.encode(scope, 7, "node:abc")
	if token == "" || strings.Contains(token, "node:abc") {
		t.Fatalf("token should be opaque and non-empty: %q", token)
	}
	pos, err := c.decode(scope, 7, token)
	if err != nil || pos != "node:abc" {
		t.Fatalf("decode: %q %v", pos, err)
	}
	if pos, err := c.decode(scope, 7, ""); err != nil || pos != "" {
		t.Fatalf("empty token is the first page: %q %v", pos, err)
	}
	if c.encode(scope, 7, "") != "" {
		t.Fatal("empty position must encode to no cursor")
	}
}

func TestCursorRejectsTamperingScopeAndGeneration(t *testing.T) {
	c := testCodec()
	scope := c.scope("list", "repo", "node", "")
	token := c.encode(scope, 3, "node:abc")

	if _, err := c.decode(scope, 4, token); !errors.Is(err, deployment.ErrStaleDeployment) {
		t.Fatalf("generation mismatch: %v", err)
	}
	if _, err := c.decode(c.scope("list", "other", "node", ""), 3, token); !errors.Is(err, deployment.ErrInvalidRequest) {
		t.Fatalf("scope mismatch: %v", err)
	}
	body, sig, _ := strings.Cut(token, ".")
	if _, err := c.decode(scope, 3, body+"x."+sig); !errors.Is(err, deployment.ErrInvalidRequest) {
		t.Fatalf("tampered body: %v", err)
	}
	other := cursorCodec{key: []byte("ffffffffffffffffffffffffffffffff"), domain: c.domain, maxBytes: c.maxBytes}
	if _, err := other.decode(scope, 3, token); !errors.Is(err, deployment.ErrInvalidRequest) {
		t.Fatalf("wrong key: %v", err)
	}
	if _, err := c.decode(scope, 3, "no-dot"); !errors.Is(err, deployment.ErrInvalidRequest) {
		t.Fatalf("malformed: %v", err)
	}
	c.maxBytes = 10
	if _, err := c.decode(scope, 3, token); !errors.Is(err, deployment.ErrLimitExceeded) {
		t.Fatalf("too long: %v", err)
	}
}
