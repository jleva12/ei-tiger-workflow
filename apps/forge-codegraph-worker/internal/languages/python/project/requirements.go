package project

import (
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"regexp"
	"sort"
	"strings"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"github.com/pelletier/go-toml/v2"
)

// The third-party packages a checkout declares, read from its manifests
// without running anything: PEP 621 and PEP 735 tables, Poetry, PDM and uv
// settings, requirements files, setup.cfg, a literal install_requires in
// setup.py and Pipfile. Lockfiles pin versions. Requirements that point at
// a path, a URL or a VCS, and the checkout's own projects, are not packages
// to install; a requirement is a name, extras, versions and markers only.

// maxRequirements bounds what one checkout can ask to install.
const maxRequirements = 2000

// declared is what the manifests ask for.
type declared struct {
	Requirements []string // PEP 508 requirements, one package each
	Pins         []string // name==version from lockfiles
	Local        map[string]bool
	Notes        []string
}

var (
	requirementName = regexp.MustCompile(`^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(\[[A-Za-z0-9._,\s-]*\])?\s*(.*)$`)
	versionSpec     = regexp.MustCompile(`^\(?\s*(?:(?:===|==|!=|<=|>=|~=|<|>)\s*[A-Za-z0-9.*+!_-]+\s*,?\s*)+\)?$`)
	literalString   = regexp.MustCompile(`["']([^"'\n]+)["']`)
	installRequires = regexp.MustCompile(`(?s)install_requires\s*=\s*\[(.*?)\]`)
	pep440          = regexp.MustCompile(`^[0-9][A-Za-z0-9.+!_-]*$`)
)

// normalize is a package name as PEP 503 compares them.
func normalize(name string) string {
	var b strings.Builder
	dash := false
	for _, r := range strings.ToLower(name) {
		if r == '-' || r == '_' || r == '.' {
			dash = true
			continue
		}
		if dash && b.Len() > 0 {
			b.WriteByte('-')
		}
		dash = false
		b.WriteRune(r)
	}
	return b.String()
}

// requirement cleans a PEP 508 requirement to what an installer may be given:
// a name, extras, version specifiers and markers. It returns false for
// anything else: a direct reference (name @ url), a path, an option, a VCS
// URL.
func requirement(line string) (string, string, bool) {
	line = strings.TrimSpace(line)
	if i := strings.Index(line, " #"); i >= 0 {
		line = strings.TrimSpace(line[:i])
	}
	// Per-requirement options of a requirements file (--hash=...) are not
	// part of the requirement.
	if i := strings.Index(line, " --"); i >= 0 {
		line = strings.TrimSpace(line[:i])
	}
	line = strings.TrimSpace(strings.TrimSuffix(line, "\\"))
	if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, "-") || strings.ContainsAny(line, "@\n\r\x00") || strings.Contains(line, "://") || len(line) > 512 {
		return "", "", false
	}
	m := requirementName.FindStringSubmatch(line)
	if m == nil {
		return "", "", false
	}
	rest := strings.TrimSpace(m[3])
	version, markers, _ := strings.Cut(rest, ";")
	version = strings.TrimSpace(version)
	if version != "" && !versionSpec.MatchString(version) {
		return "", "", false
	}
	out := m[1] + strings.ReplaceAll(m[2], " ", "")
	if version != "" {
		out += " " + strings.Trim(version, "() ")
	}
	if markers = strings.TrimSpace(markers); markers != "" {
		if strings.ContainsAny(markers, "\\`$") {
			return "", "", false
		}
		out += " ; " + markers
	}
	return out, normalize(m[1]), true
}

// declaredPackages reads the requirements of the checkout's root project and
// of the given project directories, and the pins of their lockfiles. Every
// file read is written to h.
func declaredPackages(root *os.Root, dirs []string, limits bc.Limits, h io.Writer) (declared, error) {
	d := declared{Local: map[string]bool{}}
	seen := map[string]bool{}
	pinned := map[string]string{}
	var elsewhere []string
	add := func(source, line string) {
		req, _, ok := requirement(line)
		if !ok {
			// Options, comments and the checkout's own paths say nothing to
			// install; a URL or VCS requirement is a package left out.
			trimmed := strings.TrimSpace(line)
			if trimmed != "" && !strings.HasPrefix(trimmed, "#") && !strings.HasPrefix(trimmed, "-") && !strings.HasPrefix(trimmed, ".") && !strings.HasPrefix(trimmed, "/") && !strings.Contains(trimmed, "file:") {
				elsewhere = append(elsewhere, shorten(trimmed, 80)+" in "+source)
			}
			return
		}
		if seen[req] || len(d.Requirements) >= maxRequirements {
			return
		}
		seen[req] = true
		d.Requirements = append(d.Requirements, req)
	}
	read := func(name string) ([]byte, error) {
		data, readable, err := readManifest(root, name, limits, h)
		if err != nil || !readable {
			return nil, err
		}
		return data, nil
	}
	for _, dir := range append([]string{"."}, dirs...) {
		at := func(name string) string {
			if dir == "." {
				return name
			}
			return dir + "/" + name
		}
		if data, err := read(at("pyproject.toml")); err != nil {
			return d, err
		} else if data != nil {
			pyprojectRequirements(at("pyproject.toml"), data, add, d.Local, &d.Notes)
		}
		if data, err := read(at("setup.cfg")); err != nil {
			return d, err
		} else if data != nil {
			setupCfgRequirements(at("setup.cfg"), string(data), add, d.Local)
		}
		if data, err := read(at("setup.py")); err != nil {
			return d, err
		} else if data != nil {
			if m := installRequires.FindStringSubmatch(string(data)); m != nil {
				for _, s := range literalString.FindAllStringSubmatch(m[1], -1) {
					add(at("setup.py"), s[1])
				}
			}
		}
		if data, err := read(at("Pipfile")); err != nil {
			return d, err
		} else if data != nil {
			pipfileRequirements(at("Pipfile"), data, add, &d.Notes)
		}
		files, err := requirementFiles(root, dir)
		if err != nil {
			return d, err
		}
		for _, name := range files {
			if err := requirementsFile(root, name, limits, h, add, 0, map[string]bool{}); err != nil {
				return d, err
			}
		}
		for _, lock := range []string{"uv.lock", "poetry.lock", "pdm.lock", "Pipfile.lock"} {
			data, err := read(at(lock))
			if err != nil {
				return d, err
			}
			if data != nil {
				lockPins(lock, data, pinned)
			}
		}
	}
	if len(elsewhere) > 0 {
		shown := elsewhere[:min(len(elsewhere), 5)]
		more := ""
		if len(elsewhere) > len(shown) {
			more = fmt.Sprintf(" and %d more", len(elsewhere)-len(shown))
		}
		d.Notes = append(d.Notes, fmt.Sprintf("requirements that are not packages of the index (a URL or a repository) were not installed: %s%s", strings.Join(shown, "; "), more))
	}
	// The checkout's own projects are analysed from source, not installed.
	kept := d.Requirements[:0]
	for _, req := range d.Requirements {
		_, name, _ := requirement(req)
		if !d.Local[name] {
			kept = append(kept, req)
		}
	}
	d.Requirements = kept
	for name, version := range pinned {
		if !d.Local[name] {
			d.Pins = append(d.Pins, name+"=="+version)
		}
	}
	sort.Strings(d.Pins)
	return d, nil
}

func pyprojectRequirements(source string, data []byte, add func(string, string), local map[string]bool, notes *[]string) {
	var cfg struct {
		Project struct {
			Name                 string              `toml:"name"`
			Dependencies         []string            `toml:"dependencies"`
			OptionalDependencies map[string][]string `toml:"optional-dependencies"`
		} `toml:"project"`
		DependencyGroups map[string][]any `toml:"dependency-groups"`
		Tool             struct {
			Poetry struct {
				Name            string                    `toml:"name"`
				Dependencies    map[string]any            `toml:"dependencies"`
				DevDependencies map[string]any            `toml:"dev-dependencies"`
				Group           map[string]map[string]any `toml:"group"`
			} `toml:"poetry"`
			UV struct {
				DevDependencies []string `toml:"dev-dependencies"`
			} `toml:"uv"`
			PDM struct {
				DevDependencies map[string][]string `toml:"dev-dependencies"`
			} `toml:"pdm"`
		} `toml:"tool"`
	}
	if err := toml.Unmarshal(data, &cfg); err != nil {
		*notes = append(*notes, fmt.Sprintf("%s could not be read (%v), so its packages were not installed", source, err))
		return
	}
	for _, name := range []string{cfg.Project.Name, cfg.Tool.Poetry.Name} {
		if name != "" {
			local[normalize(name)] = true
		}
	}
	for _, r := range cfg.Project.Dependencies {
		add(source, r)
	}
	for _, extra := range sortedKeys(cfg.Project.OptionalDependencies) {
		for _, r := range cfg.Project.OptionalDependencies[extra] {
			add(source, r)
		}
	}
	for _, group := range sortedKeys(cfg.DependencyGroups) {
		for _, r := range cfg.DependencyGroups[group] {
			if s, ok := r.(string); ok {
				add(source, s)
			}
		}
	}
	for _, r := range cfg.Tool.UV.DevDependencies {
		add(source, r)
	}
	for _, group := range sortedKeys(cfg.Tool.PDM.DevDependencies) {
		for _, r := range cfg.Tool.PDM.DevDependencies[group] {
			add(source, r)
		}
	}
	poetry := []map[string]any{cfg.Tool.Poetry.Dependencies, cfg.Tool.Poetry.DevDependencies}
	for _, group := range sortedKeys(cfg.Tool.Poetry.Group) {
		if deps, ok := cfg.Tool.Poetry.Group[group]["dependencies"].(map[string]any); ok {
			poetry = append(poetry, deps)
		}
	}
	for _, deps := range poetry {
		for _, name := range sortedKeys(deps) {
			if strings.EqualFold(name, "python") {
				continue
			}
			if req, ok := poetryRequirement(name, deps[name]); ok {
				add(source, req)
			} else if table, isTable := deps[name].(map[string]any); isTable && (table["path"] != nil) {
				local[normalize(name)] = true
			}
		}
	}
}

// poetryRequirement turns a Poetry dependency into a requirement. Poetry's
// caret and tilde versions are left to the index's newest; a PEP 440
// specifier is kept. A path, git or URL dependency is not a package.
func poetryRequirement(name string, value any) (string, bool) {
	version := ""
	extras := ""
	switch v := value.(type) {
	case string:
		version = v
	case map[string]any:
		for _, key := range []string{"path", "git", "url", "file"} {
			if v[key] != nil {
				return "", false
			}
		}
		version, _ = v["version"].(string)
		if list, ok := v["extras"].([]any); ok {
			var names []string
			for _, e := range list {
				if s, ok := e.(string); ok {
					names = append(names, s)
				}
			}
			if len(names) > 0 {
				extras = "[" + strings.Join(names, ",") + "]"
			}
		}
	case []any:
		// Several constraints for different Pythons or platforms.
	default:
		return "", false
	}
	version = strings.TrimSpace(version)
	if version == "*" || strings.HasPrefix(version, "^") || strings.HasPrefix(version, "~") && !strings.HasPrefix(version, "~=") {
		version = ""
	} else if pep440.MatchString(version) {
		version = "==" + version
	}
	req, _, ok := requirement(name + extras + " " + version)
	if !ok {
		req, _, ok = requirement(name + extras)
	}
	return req, ok
}

func setupCfgRequirements(source, text string, add func(string, string), local map[string]bool) {
	section, key := "", ""
	for _, line := range strings.Split(text, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "[") && strings.HasSuffix(trimmed, "]") {
			section, key = strings.ToLower(strings.Trim(trimmed, "[]")), ""
			continue
		}
		if trimmed == "" || strings.HasPrefix(trimmed, "#") || strings.HasPrefix(trimmed, ";") {
			continue
		}
		indented := line != strings.TrimLeft(line, " \t")
		if !indented {
			k, v, ok := strings.Cut(trimmed, "=")
			if !ok {
				key = ""
				continue
			}
			key = strings.TrimSpace(k)
			if section == "metadata" && key == "name" {
				local[normalize(strings.TrimSpace(v))] = true
			}
			trimmed = strings.TrimSpace(v)
			if trimmed == "" {
				continue
			}
		}
		if (section == "options" && key == "install_requires") || section == "options.extras_require" {
			add(source, trimmed)
		}
	}
}

func pipfileRequirements(source string, data []byte, add func(string, string), notes *[]string) {
	var cfg map[string]any
	if err := toml.Unmarshal(data, &cfg); err != nil {
		*notes = append(*notes, fmt.Sprintf("%s could not be read (%v), so its packages were not installed", source, err))
		return
	}
	for _, section := range []string{"packages", "dev-packages"} {
		deps, _ := cfg[section].(map[string]any)
		for _, name := range sortedKeys(deps) {
			value := deps[name]
			if s, ok := value.(string); ok && s != "*" {
				if req, ok := poetryRequirement(name, map[string]any{"version": s}); ok {
					add(source, req)
				}
				continue
			}
			if req, ok := poetryRequirement(name, value); ok {
				add(source, req)
			}
		}
	}
}

// requirementFiles are a project's requirements files: requirements*.txt
// and *-requirements.txt beside its manifest and in a requirements
// directory.
func requirementFiles(root *os.Root, dir string) ([]string, error) {
	var out []string
	for _, sub := range []string{dir, path.Join(dir, "requirements")} {
		entries, err := fs.ReadDir(root.FS(), sub)
		if err != nil {
			continue
		}
		for _, e := range entries {
			name := strings.ToLower(e.Name())
			if !e.Type().IsRegular() || !strings.HasSuffix(name, ".txt") {
				continue
			}
			if strings.HasPrefix(name, "requirements") || strings.HasSuffix(name, "requirements.txt") || sub != dir {
				out = append(out, path.Join(sub, e.Name()))
			}
		}
	}
	return out, nil
}

// requirementsFile reads a requirements file and the ones it includes with
// -r inside the checkout; its index and other options are the operator's to
// set, not the checkout's.
func requirementsFile(root *os.Root, name string, limits bc.Limits, h io.Writer, add func(string, string), depth int, visited map[string]bool) error {
	if depth > 4 || visited[name] {
		return nil
	}
	visited[name] = true
	data, readable, err := readManifest(root, name, limits, h)
	if err != nil || !readable {
		return err
	}
	text := strings.ReplaceAll(string(data), "\\\n", " ")
	for _, line := range strings.Split(text, "\n") {
		trimmed := strings.TrimSpace(line)
		for _, flag := range []string{"-r ", "--requirement ", "-r", "--requirement="} {
			if rest, ok := strings.CutPrefix(trimmed, flag); ok && rest != "" {
				included := path.Clean(path.Join(path.Dir(name), strings.TrimSpace(rest)))
				if fs.ValidPath(included) && !strings.HasPrefix(included, "../") {
					if err := requirementsFile(root, included, limits, h, add, depth+1, visited); err != nil {
						return err
					}
				}
				trimmed = ""
				break
			}
		}
		if trimmed != "" {
			add(name, trimmed)
		}
	}
	return nil
}

// lockPins reads the versions a lockfile pins for packages from an index.
func lockPins(name string, data []byte, pins map[string]string) {
	if name == "Pipfile.lock" {
		var lock map[string]map[string]struct {
			Version string `json:"version"`
		}
		if json.Unmarshal(data, &lock) != nil {
			return
		}
		for _, section := range []string{"default", "develop"} {
			for pkg, entry := range lock[section] {
				if v := strings.TrimPrefix(entry.Version, "=="); pep440.MatchString(v) {
					pins[normalize(pkg)] = v
				}
			}
		}
		return
	}
	var lock struct {
		Package []struct {
			Name    string         `toml:"name"`
			Version string         `toml:"version"`
			Source  map[string]any `toml:"source"`
		} `toml:"package"`
	}
	if toml.Unmarshal(data, &lock) != nil {
		return
	}
	for _, p := range lock.Package {
		if p.Name == "" || !pep440.MatchString(p.Version) {
			continue
		}
		if name == "uv.lock" && p.Source != nil && p.Source["registry"] == nil {
			continue // editable, virtual, path, git or URL
		}
		if name == "poetry.lock" && p.Source != nil {
			if kind, _ := p.Source["type"].(string); kind == "directory" || kind == "file" || kind == "git" || kind == "url" {
				continue
			}
		}
		pins[normalize(p.Name)] = p.Version
	}
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func shorten(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
