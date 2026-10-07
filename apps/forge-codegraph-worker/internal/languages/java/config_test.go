package java

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// javaSettings builds a configuration object around the test JDK. The JDK is
// mandatory: javac is the only binding authority.
func javaSettings(t *testing.T, extra string) json.RawMessage {
	t.Helper()
	home := os.Getenv("CODEGRAPH_TEST_JAVA_HOME")
	if home == "" {
		t.Skip("requires CODEGRAPH_TEST_JAVA_HOME")
	}
	dir := t.TempDir()
	base := `"java_home":` + jsonString(home) + `,"cache_dir":` + jsonString(filepath.Join(dir, "cache")) + `,"work_dir":` + jsonString(filepath.Join(dir, "work"))
	if extra != "" {
		base += "," + extra
	}
	return json.RawMessage("{" + base + "}")
}

func jsonString(s string) string {
	b, _ := json.Marshal(s)
	return string(b)
}

func TestJavaConfigurationOwnedByAdapter(t *testing.T) {
	if cfg := DefaultConfig(); cfg.Release != 0 || cfg.FallbackRelease != 21 {
		t.Fatal(cfg)
	}
	for _, mode := range []string{"auto", "manifest", "maven", "gradle"} {
		if _, err := configure(javaSettings(t, ""), mode); err != nil {
			t.Fatal(mode, err)
		}
		if _, err := configure(javaSettings(t, `"release":21`), mode); err == nil {
			t.Fatal("explicit release overrides build metadata")
		}
	}
	configured, err := configure(javaSettings(t, `"release":17,"fallback_release":11`), "syntax")
	if err != nil || configured.SyntaxProfile.Version != "17" || configured.Resolver == nil {
		t.Fatal("wrong explicit profile", err)
	}
	for _, raw := range []string{`null`, `[]`, `{"release":"21"}`, `{"release":21.5}`, `{"unexpected":1}`, `{"release":21} {}`, `{"release":null}`} {
		if _, err := configure(json.RawMessage(raw), "syntax"); err == nil {
			t.Fatal("invalid config accepted", raw)
		}
	}
	for _, extra := range []string{`"release":22`, `"fallback_release":22`} {
		if _, err := configure(javaSettings(t, extra), "syntax"); err == nil {
			t.Fatal("unsupported release accepted", extra)
		}
	}
	if _, err := configure(javaSettings(t, ""), "syntax"); err == nil {
		t.Fatal("syntax version must be explicit")
	}
	// A JDK is required; relative or missing paths are rejected before any I/O.
	if _, err := configure(json.RawMessage(`{"fallback_release":21}`), "auto"); err == nil {
		t.Fatal("configuration without java_home accepted")
	}
	if _, err := configure(json.RawMessage(`{"java_home":"jdk","cache_dir":"/tmp/c","work_dir":"/tmp/w"}`), "auto"); err == nil {
		t.Fatal("relative java_home accepted")
	}
}

func TestBuildJavaHomesAreMajorVersionsWithAbsolutePaths(t *testing.T) {
	base := Config{FallbackRelease: 21, JavaHome: "/opt/jdk", CacheDir: "/tmp/c", WorkDir: "/tmp/w"}
	for _, homes := range []map[string]string{{"x": "/opt/jdk8"}, {"08": "/opt/jdk8"}, {"7": "/opt/jdk7"}, {"8": "jdk8"}} {
		cfg := base
		cfg.BuildJavaHomes = homes
		if err := cfg.Validate("maven-resolved"); err == nil {
			t.Fatal("invalid build_java_homes accepted", homes)
		}
	}
	cfg := base
	cfg.BuildJavaHomes = map[string]string{"8": "/opt/jdk8", "17": "/opt/jdk17"}
	if err := cfg.Validate("maven-resolved"); err != nil {
		t.Fatal(err)
	}
	homes, err := cfg.buildJavaHomes()
	if err != nil || len(homes) != 2 || homes[8] != "/opt/jdk8" || homes[17] != "/opt/jdk17" {
		t.Fatal(homes, err)
	}
}
