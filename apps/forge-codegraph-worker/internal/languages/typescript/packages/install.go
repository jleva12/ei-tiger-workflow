// Package packages installs the npm packages a TypeScript project declares,
// so the compiler tier can read their declarations. npm runs with every
// lifecycle script disabled, so no package code runs; only the operator's
// registry is used, since the checkout's .npmrc is never read and lockfile
// entries fetched from anywhere else are dropped; an installation keeps only
// what the compiler reads, is cached by what it was asked for, and a package
// that cannot be installed is left out and named.
package packages

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net/url"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"ei-aitiger-codegraph/worker/internal/scratch"
	"go.yaml.in/yaml/v3"
)

// format versions the cached installation layout and marker.
const format = "1"

// marker records a finished installation inside its directory.
const marker = ".codegraph-installed.json"

// retryAfter is how long an installation that could not finish (the
// registry unreachable, out of time) is not tried again.
const retryAfter = 6 * time.Hour

// DefaultRegistry is npm's public registry.
const DefaultRegistry = "https://registry.npmjs.org/"

// maxAttempts bounds the installs of one project: each failed one leaves
// out the package that failed it.
const maxAttempts = 12

type Config struct {
	NPM        string // the npm executable
	NPMVersion string // its version, part of what an installation is keyed by
	Registry   string // empty for DefaultRegistry
	CacheDir   string // absolute; installations and npm's download cache
	MaxBytes   int64  // an installation larger than this, after pruning, is not used
	Timeout    time.Duration
	Exclude    []string // package names never installed
}

// Project is one npm project of a checkout: its package.json, those of its
// workspace packages, and its lockfile, by path relative to its directory.
type Project struct {
	Dir   string
	Files map[string][]byte
}

type Failure struct {
	Package string `json:"package"`
	Reason  string `json:"reason"`
}

type Result struct {
	Dir    string    `json:"-"`
	Digest string    `json:"digest"`
	Failed []Failure `json:"failed,omitempty"`
	// Unlocked is set when the lockfile could not be installed as written
	// and versions were resolved from the manifests' ranges.
	Unlocked bool `json:"unlocked,omitempty"`
}

// Install returns the directory the project's packages are installed in,
// laid out as the project (node_modules at its root and, when npm needs
// them, in its workspace packages), installing them unless an earlier run
// did. An error means nothing could be installed.
func Install(ctx context.Context, c Config, p Project) (Result, error) {
	if !filepath.IsAbs(c.CacheDir) {
		return Result{}, errors.New("the package cache directory must be absolute")
	}
	registry := c.Registry
	if registry == "" {
		registry = DefaultRegistry
	}
	host, err := registryHost(registry)
	if err != nil {
		return Result{}, err
	}
	if err := os.MkdirAll(c.CacheDir, 0o700); err != nil {
		return Result{}, err
	}
	scratch.Sweep(c.CacheDir, "tmp-", 24*time.Hour)
	scratch.Sweep(c.CacheDir, "npm-", 30*24*time.Hour)
	scratch.Sweep(c.CacheDir, "failed-", retryAfter)
	boundDownloads(filepath.Join(c.CacheDir, "npm-cache"), 4*c.MaxBytes)
	excluded := map[string]bool{}
	for _, name := range c.Exclude {
		excluded[strings.ToLower(name)] = true
	}
	files, dropped, err := sanitize(p.Files, host, excluded)
	if err != nil {
		return Result{}, err
	}
	if _, ok := files["package.json"]; !ok {
		return Result{}, errors.New("the project has no package.json")
	}
	names := make([]string, 0, len(files))
	for name := range files {
		names = append(names, name)
	}
	sort.Strings(names)
	h := sha256.New()
	fmt.Fprintf(h, "format=%s\nnpm=%s\nregistry=%s\nexclude=%s\n", format, c.NPMVersion, registry, strings.Join(sortedKeys(excluded), ","))
	for _, name := range names {
		fmt.Fprintf(h, "%d:%s:%d:", len(name), name, len(files[name]))
		h.Write(files[name])
	}
	key := hex.EncodeToString(h.Sum(nil))[:40]
	previous, found := latest(c.CacheDir, key)
	if found {
		now := time.Now()
		_ = os.Chtimes(previous.Dir, now, now)
		return previous, nil
	}
	failed := filepath.Join(c.CacheDir, "failed-"+key)
	if info, err := os.Stat(failed); err == nil && time.Since(info.ModTime()) < retryAfter {
		why, _ := os.ReadFile(failed)
		return Result{}, fmt.Errorf("%s (tried %s ago; tried again %s after that)", why, time.Since(info.ModTime()).Round(time.Minute), retryAfter)
	}
	remember := func(why string) { _ = os.WriteFile(failed, []byte(why), 0o600) }
	work, err := os.MkdirTemp(c.CacheDir, "tmp-"+key+"-")
	if err != nil {
		return Result{}, err
	}
	defer os.RemoveAll(work)
	timeout := c.Timeout
	if timeout <= 0 {
		timeout = 15 * time.Minute
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	site := filepath.Join(work, "project")
	result := Result{}
	for name, reason := range dropped {
		result.Failed = append(result.Failed, Failure{Package: name, Reason: reason})
	}
	locked := files["package-lock.json"] != nil || files["npm-shrinkwrap.json"] != nil
	if len(dropped) > 0 {
		result.Unlocked = locked
	}
	var why string
	for attempt := 0; ; attempt++ {
		if err := writeProject(site, files); err != nil {
			return Result{}, err
		}
		command := "install"
		if locked && !result.Unlocked {
			command = "ci"
		}
		why, err = c.npm(ctx, site, registry, command)
		if err == nil {
			break
		}
		if ctx.Err() != nil {
			remember(fmt.Sprintf("installing packages took longer than %s", timeout))
			return Result{}, fmt.Errorf("installing packages took longer than %s", timeout)
		}
		if transient(why) {
			remember("the package registry could not be reached: " + why)
			return Result{}, fmt.Errorf("the package registry could not be reached: %s", why)
		}
		if attempt >= maxAttempts {
			return Result{}, fmt.Errorf("npm could not install the packages: %s", why)
		}
		name := failingPackage(why)
		switch {
		case name != "" && !excluded[strings.ToLower(name)]:
			// Leave the package out, and install what is left from the
			// manifests' ranges.
			excluded[strings.ToLower(name)] = true
			result.Failed = append(result.Failed, Failure{Package: name, Reason: why})
			files, _, err = sanitize(p.Files, host, excluded)
			if err != nil {
				return Result{}, err
			}
			result.Unlocked = locked
		case locked && !result.Unlocked:
			result.Unlocked = true // the lockfile as written does not install
		default:
			return Result{}, fmt.Errorf("npm could not install the packages: %s", why)
		}
	}
	kept, err := prune(site)
	if err != nil {
		return Result{}, err
	}
	if c.MaxBytes > 0 && kept > c.MaxBytes {
		return Result{}, fmt.Errorf("the packages take %d MiB, more than the %d MiB allowed", kept>>20, c.MaxBytes>>20)
	}
	result.Digest, err = Digest(ctx, site)
	if err != nil {
		return Result{}, err
	}
	sort.Slice(result.Failed, func(i, j int) bool { return result.Failed[i].Package < result.Failed[j].Package })
	data, err := json.Marshal(result)
	if err != nil {
		return Result{}, err
	}
	if err := os.WriteFile(filepath.Join(site, marker), data, 0o600); err != nil {
		return Result{}, err
	}
	// An installation is never changed once finished, because a run lays
	// it beside sources it analyses later; the same packages installed by
	// another worker first are the same directory.
	dir := filepath.Join(c.CacheDir, "npm-"+key+"-"+result.Digest[:16])
	if err := os.Rename(site, dir); err != nil {
		existing, ok := read(dir)
		if !ok {
			return Result{}, err
		}
		result = existing
	}
	_ = os.Remove(failed)
	result.Dir = dir
	now := time.Now()
	_ = os.Chtimes(dir, now, now)
	return result, nil
}

// npm runs one install in dir and returns why it failed.
func (c Config) npm(ctx context.Context, dir, registry, command string) (string, error) {
	args := []string{command, "--ignore-scripts", "--no-audit", "--no-fund", "--legacy-peer-deps", "--loglevel=error", "--no-update-notifier",
		"--registry=" + registry, "--cache=" + filepath.Join(c.CacheDir, "npm-cache"), "--prefer-offline"}
	if command == "install" {
		args = append(args, "--no-save")
	}
	cmd := exec.CommandContext(ctx, c.NPM, args...)
	cmd.Dir = dir
	// npm reads no .npmrc of the checkout: the project's files are copied
	// without it. Scripts are off for install, and for every package.
	cmd.Env = append(os.Environ(), "npm_config_ignore_scripts=true", "npm_config_audit=false", "npm_config_fund=false", "npm_config_update_notifier=false", "NO_COLOR=1")
	var output bytes.Buffer
	cmd.Stdout, cmd.Stderr = &output, &output
	if err := cmd.Run(); err != nil {
		if ctx.Err() != nil {
			return "", ctx.Err()
		}
		return reason(output.String(), err), errors.New("npm failed")
	}
	return "", nil
}

func registryHost(registry string) (string, error) {
	u, err := url.Parse(registry)
	if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" {
		return "", fmt.Errorf("the npm registry %q is not an http(s) URL", registry)
	}
	return u.Host, nil
}

// writeProject lays the project's files out in dir, replacing what an
// earlier attempt left.
func writeProject(dir string, files map[string][]byte) error {
	if err := os.RemoveAll(dir); err != nil {
		return err
	}
	for name, data := range files {
		p := filepath.Join(dir, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
			return err
		}
		if err := os.WriteFile(p, data, 0o600); err != nil {
			return err
		}
	}
	return nil
}

// Dependency sections of a manifest.
var dependencySections = []string{"dependencies", "devDependencies", "optionalDependencies", "peerDependencies"}

// sanitize is what npm is given of the project: its manifests without
// dependencies that are not packages of the registry (a path, a git or
// tarball URL, a pnpm catalog) or are excluded, workspace: ranges as the
// workspace packages they name, and its lockfile without packages fetched
// from another host. It returns the packages it left out and why.
func sanitize(files map[string][]byte, host string, excluded map[string]bool) (map[string][]byte, map[string]string, error) {
	out := map[string][]byte{}
	dropped := map[string]string{}
	manifests := map[string]map[string]any{}
	for name, data := range files {
		if path.Base(name) != "package.json" {
			continue
		}
		var manifest map[string]any
		if err := json.Unmarshal(data, &manifest); err != nil {
			if name == "package.json" {
				return nil, nil, fmt.Errorf("the project's package.json is not JSON: %v", err)
			}
			continue // a workspace package that does not parse is left out
		}
		manifests[name] = manifest
	}
	root := manifests["package.json"]
	members := workspaceMembers(manifests)
	catalogs := readCatalogs(files[pnpmWorkspace], root)
	for name, manifest := range manifests {
		delete(manifest, "packageManager")
		delete(manifest, "scripts")
		delete(manifest, "engineStrict")
		for _, section := range dependencySections {
			deps, _ := manifest[section].(map[string]any)
			for dep, raw := range deps {
				spec, _ := raw.(string)
				switch {
				case excluded[strings.ToLower(dep)]:
					delete(deps, dep)
				case strings.HasPrefix(spec, "workspace:"):
					// A link to a workspace package npm links by name; one
					// the checkout does not hold is its own code, not a
					// registry package of the same name.
					if _, ok := members[dep]; ok {
						deps[dep] = "*"
					} else {
						delete(deps, dep)
					}
				case strings.HasPrefix(spec, "catalog:"):
					deps[dep] = catalogs.version(dep, strings.TrimPrefix(spec, "catalog:"))
				case strings.HasPrefix(spec, "file:") || strings.HasPrefix(spec, "link:") || strings.HasPrefix(spec, "portal:"):
					// A path of the checkout: its own code, analysed
					// from source (a workspace package is linked).
					delete(deps, dep)
				case !registrySpec(spec):
					delete(deps, dep)
					dropped[dep] = "it is not a package of the registry (" + truncate(spec, 80) + ")"
				}
			}
		}
		// Overrides pin versions of the registry; one that points
		// elsewhere would fetch from there.
		for _, field := range []string{"overrides", "resolutions"} {
			if raw, ok := manifest[field]; ok {
				written, _ := json.Marshal(raw)
				for _, protocol := range []string{"file:", "link:", "git", "http:", "https:", "portal:", "patch:"} {
					if bytes.Contains(written, []byte(`"`+protocol)) {
						delete(manifest, field)
						break
					}
				}
			}
		}
		if name == "package.json" {
			// npm is given the workspace packages discovery found, by
			// directory: pnpm lists them in pnpm-workspace.yaml, which npm
			// does not read, and Yarn and Bun may write an object npm does
			// not accept.
			delete(manifest, "workspaces")
			delete(manifest, "catalog")
			delete(manifest, "catalogs")
			if len(members) > 0 {
				dirs := make([]string, 0, len(members))
				for _, dir := range members {
					dirs = append(dirs, dir)
				}
				sort.Strings(dirs)
				manifest["workspaces"] = dirs
			}
		}
	}
	for name, manifest := range manifests {
		data, _ := json.Marshal(manifest)
		out[name] = data
	}
	for name, data := range files {
		if name != "package-lock.json" && name != "npm-shrinkwrap.json" {
			continue
		}
		var lock map[string]any
		if json.Unmarshal(data, &lock) != nil {
			continue // an unreadable lockfile is resolved from the manifests
		}
		if version, _ := lock["lockfileVersion"].(float64); version < 2 {
			continue
		}
		packages, _ := lock["packages"].(map[string]any)
		for key, raw := range packages {
			entry, _ := raw.(map[string]any)
			resolved, _ := entry["resolved"].(string)
			// The root, a workspace package, and the link to one.
			if link, _ := entry["link"].(bool); key == "" || resolved == "" || link || !strings.Contains(key, "node_modules/") {
				continue
			}
			pkg := key[strings.LastIndex(key, "node_modules/")+len("node_modules/"):]
			u, err := url.Parse(resolved)
			switch {
			case excluded[strings.ToLower(pkg)]:
				delete(packages, key)
			case err != nil || u.Scheme != "https" || (u.Host != host && u.Host != "registry.npmjs.org" && u.Host != "registry.yarnpkg.com"):
				delete(packages, key)
				if _, ok := dropped[pkg]; !ok && !strings.Contains(key, "node_modules/"+pkg+"/node_modules/") {
					dropped[pkg] = "the lockfile fetches it from " + truncate(u.Host, 80) + ", not the registry"
				}
			}
		}
		data, _ = json.Marshal(lock)
		out[name] = data
	}
	// A package the lockfile could not keep is left out of the manifests
	// too, so what remains resolves.
	for name, data := range out {
		if path.Base(name) != "package.json" || len(dropped) == 0 {
			continue
		}
		var manifest map[string]any
		if json.Unmarshal(data, &manifest) != nil {
			continue
		}
		for _, section := range dependencySections {
			deps, _ := manifest[section].(map[string]any)
			for dep := range dropped {
				delete(deps, dep)
			}
		}
		out[name], _ = json.Marshal(manifest)
	}
	return out, dropped, nil
}

// pnpmWorkspace is pnpm's workspace file: its packages and catalogs.
const pnpmWorkspace = "pnpm-workspace.yaml"

// workspaceMembers are the project's workspace packages by name, with
// their directories: every package.json below the root that discovery
// passed with it. A package without a name gets one, since npm names every
// workspace; of two with one name, the first directory keeps it.
func workspaceMembers(manifests map[string]map[string]any) map[string]string {
	names := make([]string, 0, len(manifests))
	for name := range manifests {
		if name != "package.json" {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	members := map[string]string{}
	for i, name := range names {
		dir := path.Dir(name)
		pkg, _ := manifests[name]["name"].(string)
		if pkg == "" {
			pkg = fmt.Sprintf("codegraph-workspace-%d", i+1)
			manifests[name]["name"] = pkg
		}
		if _, taken := members[pkg]; !taken {
			members[pkg] = dir
		}
	}
	return members
}

// catalogs are the version catalogs a workspace defines once and its
// packages refer to as catalog: (the default) or catalog:<name>, by
// catalog and dependency.
type catalogs map[string]map[string]string

// readCatalogs reads pnpm's catalogs from pnpm-workspace.yaml and Bun's from
// the root manifest (catalog and catalogs at its top level or in its
// workspaces object).
func readCatalogs(pnpm []byte, root map[string]any) catalogs {
	c := catalogs{}
	add := func(catalog string, entries map[string]string) {
		if c[catalog] == nil {
			c[catalog] = map[string]string{}
		}
		for dep, version := range entries {
			c[catalog][dep] = version
		}
	}
	if len(pnpm) > 0 {
		var workspace struct {
			Catalog  map[string]string            `yaml:"catalog"`
			Catalogs map[string]map[string]string `yaml:"catalogs"`
		}
		if yaml.Unmarshal(pnpm, &workspace) == nil {
			add("default", workspace.Catalog)
			for name, entries := range workspace.Catalogs {
				add(name, entries)
			}
		}
	}
	versions := func(raw any) map[string]string {
		out := map[string]string{}
		entries, _ := raw.(map[string]any)
		for dep, v := range entries {
			if version, ok := v.(string); ok {
				out[dep] = version
			}
		}
		return out
	}
	for _, holder := range []any{root, root["workspaces"]} {
		object, _ := holder.(map[string]any)
		if object == nil {
			continue
		}
		add("default", versions(object["catalog"]))
		named, _ := object["catalogs"].(map[string]any)
		for name, entries := range named {
			add(name, versions(entries))
		}
	}
	return c
}

// version is the range a catalog gives a dependency, or the newest version
// when the catalog does not name it or names something npm cannot fetch.
func (c catalogs) version(dep, catalog string) string {
	if catalog == "" {
		catalog = "default"
	}
	if version, ok := c[catalog][dep]; ok && registrySpec(version) {
		return version
	}
	return "*"
}

// registrySpec reports a dependency range npm resolves from a registry: a
// version range, a dist-tag, or an npm: alias.
func registrySpec(spec string) bool {
	spec = strings.TrimSpace(spec)
	switch {
	case spec == "", spec == "*", spec == "latest":
		return true
	case strings.HasPrefix(spec, "npm:"):
		return true
	case strings.Contains(spec, "://"), strings.Contains(spec, ":"), strings.HasPrefix(spec, "."), strings.HasPrefix(spec, "/"), strings.HasPrefix(spec, "~/"):
		return false
	case strings.Contains(spec, "/") && !strings.ContainsAny(spec, " <>=^~|"):
		return false // user/repo, a GitHub shorthand
	}
	return true
}

var (
	notFoundURL  = regexp.MustCompile(`(?:404|403|401)[^\n]*GET https?://[^\s]+?/(@[^/\s%]+(?:%2[fF]|/)[^/\s]+|[^@/\s][^/\s]*)(?:/-/|\s|$)`)
	notInReg     = regexp.MustCompile(`'((?:@[^/'\s]+/)?[^@'\s]+)@[^']*' is not in (?:this|the npm) registry`)
	noVersion    = regexp.MustCompile(`No matching version found for ((?:@[^/\s]+/)?[^@\s]+)@`)
	integrityFor = regexp.MustCompile(`(?:EINTEGRITY|integrity)[^\n]*?((?:@[^/\s]+/)?[a-z0-9][a-z0-9._-]*)-\d+\.\d+\.\d+[^\s]*\.tgz`)
)

// failingPackage is the package an npm failure names, or "".
func failingPackage(output string) string {
	for _, re := range []*regexp.Regexp{notInReg, noVersion, notFoundURL, integrityFor} {
		if m := re.FindStringSubmatch(output); m != nil {
			name, err := url.PathUnescape(m[1])
			if err != nil {
				name = m[1]
			}
			return name
		}
	}
	return ""
}

// reason is the line of npm's output that says why it failed.
func reason(output string, err error) string {
	var best string
	for _, line := range strings.Split(output, "\n") {
		line = strings.TrimSpace(strings.TrimPrefix(strings.TrimPrefix(strings.TrimSpace(line), "npm error"), "npm ERR!"))
		if line == "" || strings.HasPrefix(line, "A complete log") || strings.HasPrefix(line, "code ") || strings.HasPrefix(line, "errno") {
			continue
		}
		if best == "" || strings.Contains(line, "404") || strings.Contains(line, "403") || strings.Contains(line, "No matching version") || strings.Contains(line, "is not in") {
			best = line
		}
	}
	if best == "" {
		best = err.Error()
	}
	return truncate(best, 300)
}

// transient reports a failure that says nothing about the packages: the
// registry could not be reached or answered with a server error.
func transient(why string) bool {
	lower := strings.ToLower(why)
	for _, s := range []string{"enotfound", "econnrefused", "econnreset", "etimedout", "eai_again", "socket hang up", "network", "500 internal", "502 bad gateway", "503 service", "504 gateway", "certificate"} {
		if strings.Contains(lower, s) {
			return true
		}
	}
	return false
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}

func sortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// keptSuffixes are the files of a package the compiler reads: its manifest,
// declarations and TypeScript sources, and tsconfig bases a project
// extends.
func keep(name string) bool {
	switch {
	case name == "package.json",
		strings.HasSuffix(name, ".d.ts"), strings.HasSuffix(name, ".d.mts"), strings.HasSuffix(name, ".d.cts"),
		strings.HasSuffix(name, ".ts") && !strings.HasSuffix(name, ".spec.ts") && !strings.HasSuffix(name, ".test.ts"),
		strings.HasSuffix(name, ".tsx"), strings.HasSuffix(name, ".mts"), strings.HasSuffix(name, ".cts"),
		strings.HasPrefix(name, "tsconfig") && strings.HasSuffix(name, ".json"):
		return true
	}
	return false
}

// prune keeps what the compiler reads in every node_modules of the project
// (links as they are) and returns the bytes kept.
func prune(root string) (int64, error) {
	var kept int64
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(root, p)
		inModules := strings.Contains(filepath.ToSlash(rel)+"/", "node_modules/")
		if d.IsDir() {
			if inModules && (d.Name() == ".bin" || d.Name() == ".cache") {
				if err := os.RemoveAll(p); err != nil {
					return err
				}
				return filepath.SkipDir
			}
			return nil
		}
		if !inModules || d.Type()&os.ModeSymlink != 0 {
			return nil
		}
		if !d.Type().IsRegular() || !keep(d.Name()) {
			return os.Remove(p)
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		kept += info.Size()
		return nil
	})
	return kept, err
}

// Digest fingerprints an installation: every file by path and bytes, and
// every link by what it points to.
func Digest(ctx context.Context, root string) (string, error) {
	h := sha256.New()
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		rel, _ := filepath.Rel(root, p)
		rel = filepath.ToSlash(rel)
		if rel == marker {
			return nil
		}
		switch {
		case d.Type()&os.ModeSymlink != 0:
			target, err := os.Readlink(p)
			if err != nil {
				return err
			}
			fmt.Fprintf(h, "link:%d:%s:%s\n", len(rel), rel, target)
		case d.IsDir():
			fmt.Fprintf(h, "dir:%d:%s\n", len(rel), rel)
		case d.Type().IsRegular():
			data, err := os.ReadFile(p)
			if err != nil {
				return err
			}
			fmt.Fprintf(h, "file:%d:%s:%d:", len(rel), rel, len(data))
			h.Write(data)
		}
		return nil
	})
	return hex.EncodeToString(h.Sum(nil)), err
}

// latest is the most recently used finished installation of a request.
func latest(cache, key string) (Result, bool) {
	entries, err := os.ReadDir(cache)
	if err != nil {
		return Result{}, false
	}
	var best Result
	var bestTime time.Time
	found := false
	for _, e := range entries {
		if !e.IsDir() || !strings.HasPrefix(e.Name(), "npm-"+key+"-") {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		if r, ok := read(filepath.Join(cache, e.Name())); ok && (!found || info.ModTime().After(bestTime)) {
			best, bestTime, found = r, info.ModTime(), true
		}
	}
	return best, found
}

func read(dir string) (Result, bool) {
	data, err := os.ReadFile(filepath.Join(dir, marker))
	if err != nil {
		return Result{}, false
	}
	var r Result
	if json.Unmarshal(data, &r) != nil || len(r.Digest) != 64 {
		return Result{}, false
	}
	r.Dir = dir
	return r, true
}

// boundDownloads empties npm's download cache once it grows past limit,
// measured at most once a day.
func boundDownloads(dir string, limit int64) {
	stamp := filepath.Join(dir, ".codegraph-measured")
	if info, err := os.Stat(stamp); limit <= 0 || err == nil && time.Since(info.ModTime()) < 24*time.Hour {
		return
	}
	var size int64
	_ = filepath.WalkDir(dir, func(_ string, d fs.DirEntry, err error) error {
		if err == nil && d.Type().IsRegular() {
			if info, err := d.Info(); err == nil {
				size += info.Size()
			}
		}
		return nil
	})
	if size > limit {
		_ = os.RemoveAll(dir)
	}
	if os.MkdirAll(dir, 0o700) == nil {
		_ = os.WriteFile(stamp, nil, 0o600)
	}
}
