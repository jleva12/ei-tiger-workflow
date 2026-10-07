package corporate

import (
	"ei-aitiger-codegraph/authorization"
	"testing"
)

func TestLocalDevelopmentRequiresExplicitMode(t *testing.T) {
	t.Setenv("CODEGRAPH_AUTHORIZATION_LOCAL_TENANT_ID", "tenant-a")
	for _, mode := range []string{"", "corporate", "true", "local"} {
		t.Setenv("CODEGRAPH_AUTHORIZATION_MODE", mode)
		if _, ok := NewProvider().(authorization.UnconfiguredProvider); !ok {
			t.Fatalf("mode %q enabled local login", mode)
		}
	}
	t.Setenv("CODEGRAPH_AUTHORIZATION_MODE", "local-development")
	provider, ok := NewProvider().(authorization.LocalDevelopmentProvider)
	if !ok || provider.TenantID != "tenant-a" {
		t.Fatalf("wrong local provider: %+v", provider)
	}
}
