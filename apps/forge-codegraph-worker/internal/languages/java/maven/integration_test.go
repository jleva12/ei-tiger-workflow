package maven

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

// Opt-in real Maven input preparation smoke. This executes build plugins and
// compilation inputs; the provider always disables project test execution.
func TestDubboMavenPreparationIntegration(t *testing.T) {
	if os.Getenv("CODEGRAPH_MAVEN_PREPARATION_INTEGRATION") != "1" {
		t.Skip("requires explicit Maven build-input preparation")
	}
	setting := func(key, fallback string) string {
		if value := os.Getenv(key); value != "" {
			return value
		}
		return fallback
	}
	checkout := setting("CODEGRAPH_MAVEN_CHECKOUT", "/tmp/codegraph-dubbo-authority")
	out, err := exec.Command("git", "-C", checkout, "rev-parse", "HEAD").Output()
	if err != nil {
		t.Fatal(err)
	}
	sha := strings.TrimSpace(string(out))
	if sha != "19c53bc01020fda08b19d36d8e66dab0241cca16" {
		t.Fatal("unexpected checkout commit")
	}
	provider, err := New(Config{JavaHome: setting("CODEGRAPH_MAVEN_JAVA_HOME", "/tmp/codegraph-java-tools/jdk"), MavenExecutable: setting("CODEGRAPH_MAVEN_EXECUTABLE", "/tmp/codegraph-java-tools/apache-maven-3.9.9/bin/mvn"), CacheDir: setting("CODEGRAPH_MAVEN_CACHE", "/tmp/codegraph-maven-smoke-cache"), WorkDir: setting("CODEGRAPH_MAVEN_WORK", "/tmp/codegraph-maven-smoke-work"), Timeout: 90 * time.Minute})
	if err != nil {
		t.Fatal(err)
	}
	request := bc.Request{Checkout: bc.Checkout{Path: checkout, RepositoryID: "github.com/apache/dubbo", SnapshotID: sha}, Limits: bc.DefaultLimits()}
	var build bc.BuildContext
	if model := os.Getenv("CODEGRAPH_MAVEN_OBSERVED_MODEL"); model != "" {
		build, err = provider.BuildObserved(context.Background(), request, model)
	} else {
		build, err = provider.Build(context.Background(), request)
	}
	if err != nil {
		t.Fatal(err)
	}
	body, err := json.MarshalIndent(build, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err = os.WriteFile(setting("CODEGRAPH_MAVEN_BUILD_OUTPUT", "/tmp/codegraph-java-tools/maven-build-context.json"), body, 0600); err != nil {
		t.Fatal(err)
	}
	t.Logf("status=%s modules=%d source_sets=%d artifacts=%d inputs=%d gaps=%d", build.Status, len(build.Inventory.Modules), len(build.Inventory.SourceSets), len(build.Inventory.Artifacts), len(build.Inventory.Inputs), len(build.Inventory.MissingInputs))
	if build.Status != bc.Complete {
		t.Fatalf("required build inputs remain incomplete: %+v", build.Diagnostics)
	}
}
