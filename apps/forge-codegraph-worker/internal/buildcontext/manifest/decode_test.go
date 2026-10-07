package manifest

import (
	"context"
	"errors"
	"os"
	"strings"
	"testing"

	"ei-aitiger-codegraph/pkg/buildcontext"
)

func TestDecodeDepthAndNestedDuplicates(t *testing.T) {
	for _, data := range []string{`{"inventory":{"inputs":[],"inputs":[]}}`, `{"inventory":{"Inputs":[]}}`, `{"inventory":{}} false`} {
		if _, err := decode(context.Background(), []byte(data), 128); !errors.Is(err, buildcontext.ErrInvalidInput) {
			t.Fatalf("ambiguous configuration accepted: %v", err)
		}
	}
	if _, err := decode(context.Background(), []byte(strings.Repeat("[", 129)+strings.Repeat("]", 129)), 128); !errors.Is(err, buildcontext.ErrLimitExceeded) {
		t.Fatalf("depth budget ignored: %v", err)
	}
}

func FuzzManifestDecode(f *testing.F) {
	data, err := os.ReadFile("testdata/checkout/.codegraph/build-context.json")
	if err != nil {
		f.Fatal(err)
	}
	for _, seed := range [][]byte{data, []byte(`null`), []byte(`{}`), []byte(`{"schema_version":"1.0.0","inventory":{}}`), []byte(`{"inventory":{"inputs":[],"inputs":[]}}`)} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, data []byte) {
		if len(data) > 64<<10 {
			t.Skip()
		}
		m, err := decode(context.Background(), data, 64)
		if err != nil {
			return
		}
		if m.Inventory.RecordCount() > 1_000 {
			return
		}
		// Structural validation and canonicalization must safely handle any
		// successfully decoded shape, including dangling/cyclic references.
		if _, err := buildcontext.CanonicalInventory(m.Inventory); err == nil {
			if _, err := m.Inventory.Digest(); err != nil {
				t.Fatal(err)
			}
		}
	})
}
