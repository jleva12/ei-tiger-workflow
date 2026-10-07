package deployment

import (
	"errors"
	"strings"
	"testing"
	"time"
)

func TestRequestedByIsAttributionNotIdentity(t *testing.T) {
	base := Request{
		SchemaVersion: SchemaVersion, RepositoryID: "repo-1", Branch: "main", DeploymentID: "d1",
		DeploymentSequence: 1, TargetCommitSHA: strings.Repeat("a", 40), DeployedAt: time.Unix(1, 0).UTC(),
		AnalysisConfigDigest: "sha256:" + strings.Repeat("b", 64), TriggerKind: TriggerDeployment,
	}
	anonymous, err := base.IdentityDigest()
	if err != nil {
		t.Fatal(err)
	}
	for _, user := range []string{"user-1", "user-2"} {
		r := base
		r.RequestedBy = user
		got, err := r.IdentityDigest()
		if err != nil {
			t.Fatalf("%s: %v", user, err)
		}
		if got != anonymous {
			t.Errorf("%s: identity %s, want %s", user, got, anonymous)
		}
	}
	for _, bad := range []string{" padded", "line\nbreak", strings.Repeat("u", 256)} {
		r := base
		r.RequestedBy = bad
		if err := r.Validate(); !errors.Is(err, ErrInvalidRequest) {
			t.Errorf("RequestedBy %q: Validate = %v, want ErrInvalidRequest", bad, err)
		}
	}
}
