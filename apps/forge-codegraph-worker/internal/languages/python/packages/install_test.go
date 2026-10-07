package packages

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// fakeUV installs each requested name as a package with a native library
// and a script, fails for names starting with missing (not in the index)
// and offline (the index cannot be reached), and logs its arguments.
const fakeUV = `#!/bin/sh
log="$(dirname "$0")/calls.log"
echo "$@" >> "$log"
target=""; reqs=""
while [ $# -gt 0 ]; do
  case "$1" in
    --target) target="$2"; shift ;;
    -r) reqs="$2"; shift ;;
  esac
  shift
done
names=$(sed -e 's/[][ <>=!~;].*//' "$reqs" | grep -v '^$')
for n in $names; do
  case "$n" in
    offline*) echo "error: Failed to fetch: https://pypi.org/simple/$n/" >&2; exit 2 ;;
    missing*) printf '  x No solution found when resolving dependencies:\n  Because %s was not found in the package registry and you require %s, we can conclude that your requirements are unsatisfiable.\n' "$n" "$n" >&2; exit 1 ;;
  esac
done
for n in $names; do
  mkdir -p "$target/$n" "$target/$n-1.0.dist-info" "$target/bin"
  echo "def f(): ..." > "$target/$n/__init__.py"
  echo "binary" > "$target/$n/native.so"
  mkdir -p "$target/$n/__pycache__" && echo "bytecode" > "$target/$n/__pycache__/x.pyc"
  echo "Name: $n" > "$target/$n-1.0.dist-info/METADATA"
  echo "#!/bin/sh" > "$target/bin/$n"
done
`

func fake(t *testing.T) (Config, func() []string) {
	t.Helper()
	dir := t.TempDir()
	uv := filepath.Join(dir, "uv")
	if err := os.WriteFile(uv, []byte(fakeUV), 0o700); err != nil {
		t.Fatal(err)
	}
	calls := func() []string {
		data, _ := os.ReadFile(filepath.Join(dir, "calls.log"))
		return strings.Split(strings.TrimSpace(string(data)), "\n")
	}
	return Config{UV: uv, CacheDir: filepath.Join(dir, "cache"), MaxBytes: 1 << 20, Timeout: time.Minute, Exclude: []string{"torch"}}, calls
}

func TestInstallsWheelsOnceAndKeepsWhatPyrightReads(t *testing.T) {
	c, calls := fake(t)
	r := Request{Version: "3.12", Platform: "Linux", Requirements: []string{"beta >=1", "alpha"}, Pins: []string{"alpha==1.0"}}
	got, err := Install(context.Background(), c, r)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(got.Installed, ",") != "alpha==1.0,beta==1.0" || len(got.Failed) != 0 || len(got.Digest) != 64 {
		t.Fatalf("result: %+v", got)
	}
	for _, name := range []string{"alpha/__init__.py", "alpha-1.0.dist-info/METADATA", marker} {
		if _, err := os.Stat(filepath.Join(got.Dir, name)); err != nil {
			t.Fatalf("%s was not kept: %v", name, err)
		}
	}
	for _, name := range []string{"alpha/native.so", "bin/alpha"} {
		if _, err := os.Stat(filepath.Join(got.Dir, name)); err == nil {
			t.Fatalf("%s was kept", name)
		}
	}
	log := calls()
	if len(log) != 1 {
		t.Fatalf("calls: %q", log)
	}
	for _, arg := range []string{"--no-config", "--only-binary :all:", "--python-version 3.12", "--python-platform x86_64-manylinux_2_28", "--constraints", "--overrides"} {
		if !strings.Contains(log[0], arg) {
			t.Fatalf("%s missing from %s", arg, log[0])
		}
	}
	overrides, _ := os.ReadDir(c.CacheDir)
	for _, e := range overrides {
		if strings.HasPrefix(e.Name(), "tmp-") {
			t.Fatalf("working directory %s left behind", e.Name())
		}
	}
	again, err := Install(context.Background(), c, r)
	if err != nil || again.Dir != got.Dir || again.Digest != got.Digest || len(calls()) != 1 {
		t.Fatalf("second install: %+v %v, %d calls", again, err, len(calls()))
	}
	r.Requirements = append(r.Requirements, "gamma")
	if other, err := Install(context.Background(), c, r); err != nil || other.Dir == got.Dir {
		t.Fatalf("a different request reused the installation: %+v %v", other, err)
	}
}

func TestInstallLeavesOutWhatTheIndexDoesNotHave(t *testing.T) {
	c, calls := fake(t)
	got, err := Install(context.Background(), c, Request{Version: "3.12", Platform: "Linux", Requirements: []string{"alpha", "missing-private"}, Pins: []string{"alpha==1.0"}})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(got.Installed, ",") != "alpha==1.0" || len(got.Failed) != 1 || got.Failed[0].Requirement != "missing-private" || !strings.Contains(got.Failed[0].Reason, "was not found in the package registry") || got.Retry {
		t.Fatalf("result: %+v", got)
	}
	// Pinned together, unpinned together, then each alone.
	if n := len(calls()); n != 4 {
		t.Fatalf("%d calls: %q", n, calls())
	}
	if again, err := Install(context.Background(), c, Request{Version: "3.12", Platform: "Linux", Requirements: []string{"alpha", "missing-private"}, Pins: []string{"alpha==1.0"}}); err != nil || len(again.Failed) != 1 || len(calls()) != 4 {
		t.Fatalf("a settled failure was retried: %+v %v", again, err)
	}
}

func TestAnUnreachableIndexIsTriedAgain(t *testing.T) {
	c, calls := fake(t)
	r := Request{Version: "3.12", Platform: "Linux", Requirements: []string{"offline-pkg", "alpha"}}
	if _, err := Install(context.Background(), c, r); err == nil || !strings.Contains(err.Error(), "could not be reached") {
		t.Fatalf("offline: %v", err)
	}
	// Not every run pays for it: the attempt is repeated hours later.
	if _, err := Install(context.Background(), c, r); err == nil || !strings.Contains(err.Error(), "tried again") || len(calls()) != 1 {
		t.Fatalf("an unreachable index was tried at once: %v, %d calls", err, len(calls()))
	}
	old := time.Now().Add(-retryAfter - time.Minute)
	matches, _ := filepath.Glob(filepath.Join(c.CacheDir, "failed-*"))
	for _, m := range matches {
		if err := os.Chtimes(m, old, old); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := Install(context.Background(), c, r); err == nil || len(calls()) != 2 {
		t.Fatalf("an unreachable index was not tried again later: %v, %d calls", err, len(calls()))
	}
}

func TestInstallationsPastTheBudgetAreNotUsed(t *testing.T) {
	c, _ := fake(t)
	c.MaxBytes = 8
	if _, err := Install(context.Background(), c, Request{Version: "3.12", Platform: "Linux", Requirements: []string{"alpha", "beta"}}); err == nil || !strings.Contains(err.Error(), "more than") {
		t.Fatalf("budget: %v", err)
	}
}

func TestReasonAndTransient(t *testing.T) {
	out := "  × No solution found when resolving dependencies:\n  ╰─▶ Because foo was not found in the package registry and you require foo, we can conclude that your requirements are unsatisfiable.\n\n  hint: check the name\n"
	if got := reason(out, nil); !strings.HasPrefix(got, "Because foo was not found") {
		t.Fatalf("reason: %q", got)
	}
	if transient(reason(out, nil)) || !transient("error: Failed to fetch: https://pypi.org/simple/x/") {
		t.Fatal("transient classification")
	}
}

func TestDownloadsAreBounded(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "uv-cache")
	if err := os.MkdirAll(filepath.Join(dir, "wheels"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "wheels", "big.whl"), make([]byte, 4096), 0o600); err != nil {
		t.Fatal(err)
	}
	boundDownloads(dir, 1<<20)
	if _, err := os.Stat(filepath.Join(dir, "wheels", "big.whl")); err != nil {
		t.Fatal("a cache within its bound was emptied")
	}
	_ = os.Remove(filepath.Join(dir, ".codegraph-measured"))
	boundDownloads(dir, 1024)
	if _, err := os.Stat(filepath.Join(dir, "wheels", "big.whl")); err == nil {
		t.Fatal("a cache past its bound was kept")
	}
}
