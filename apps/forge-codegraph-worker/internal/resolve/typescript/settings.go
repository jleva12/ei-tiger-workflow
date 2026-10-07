package typescript

import (
	"encoding/json"
	"fmt"
	"io/fs"
	"path/filepath"
	"sort"
	"strings"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

// Settings is the project configuration a TypeScript source set carries in
// its language options for module resolution: the path mappings of every
// tsconfig/jsconfig in the checkout and the workspace packages importable
// by name. The project discovery provider writes them; this resolver reads
// them. They are checkout-relative and deterministic, so they contribute to
// the source set's digest and a change re-resolves the set.
type Settings struct {
	Projects []Project `json:"projects,omitempty"`
	Packages []Package `json:"packages,omitempty"`
	// Installs are the packages installed for the compiler tier, one per
	// project of the checkout that declares any.
	Installs []Install `json:"installs,omitempty"`
	// Compiler fingerprints the compiler tier's bridge and the project files
	// it reads (tsconfig options beyond path mappings), so a change there
	// re-resolves the set; empty without the compiler tier.
	Compiler string `json:"compiler,omitempty"`
}

// Install is the node_modules of one project of the checkout, installed for
// the analysis: Dir is the project's directory in the checkout, Path the
// absolute directory holding what was installed for it (its node_modules,
// and those of its workspace packages at their places), SHA256 the digest
// of that tree.
type Install struct {
	Dir    string `json:"dir"`
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

// Project is one tsconfig/jsconfig: the directory it governs, its
// effective baseUrl, its path mappings with checkout-relative targets (a
// single * is substituted) and the output directory the walker skips.
type Project struct {
	Dir     string              `json:"dir"`
	BaseURL string              `json:"base_url,omitempty"`
	Paths   map[string][]string `json:"paths,omitempty"`
	OutDir  string              `json:"out_dir,omitempty"`
}

// Package is a workspace package importable by its name; Entries are the
// checkout-relative entry files its package.json names, in preference order.
type Package struct {
	Name    string   `json:"name"`
	Dir     string   `json:"dir"`
	Entries []string `json:"entries,omitempty"`
}

// Language option keys.
const (
	SettingProjects = "projects"
	SettingPackages = "packages"
	SettingInstalls = "installs"
	SettingCompiler = "compiler"
)

// Normalize sorts the settings so identical configurations encode
// identically.
func (s *Settings) Normalize() {
	sort.Slice(s.Projects, func(i, j int) bool { return s.Projects[i].Dir < s.Projects[j].Dir })
	sort.Slice(s.Packages, func(i, j int) bool { return s.Packages[i].Name < s.Packages[j].Name })
	sort.Slice(s.Installs, func(i, j int) bool { return s.Installs[i].Dir < s.Installs[j].Dir })
}

// EncodeSettings writes the settings as language options; empty sections
// are omitted.
func EncodeSettings(s Settings) (map[string]string, error) {
	s.Normalize()
	out := map[string]string{}
	if len(s.Projects) > 0 {
		data, err := json.Marshal(s.Projects)
		if err != nil {
			return nil, err
		}
		out[SettingProjects] = string(data)
	}
	if len(s.Packages) > 0 {
		data, err := json.Marshal(s.Packages)
		if err != nil {
			return nil, err
		}
		out[SettingPackages] = string(data)
	}
	if len(s.Installs) > 0 {
		data, err := json.Marshal(s.Installs)
		if err != nil {
			return nil, err
		}
		out[SettingInstalls] = string(data)
	}
	if s.Compiler != "" {
		out[SettingCompiler] = s.Compiler
	}
	return out, nil
}

// DecodeSettings reads the settings of a source set; absent options mean
// no projects and no packages.
func DecodeSettings(options map[string]string) (Settings, error) {
	var s Settings
	if raw := options[SettingProjects]; raw != "" {
		if err := json.Unmarshal([]byte(raw), &s.Projects); err != nil {
			return Settings{}, fmt.Errorf("%w: typescript projects option: %v", bc.ErrInvalidInput, err)
		}
	}
	if raw := options[SettingPackages]; raw != "" {
		if err := json.Unmarshal([]byte(raw), &s.Packages); err != nil {
			return Settings{}, fmt.Errorf("%w: typescript packages option: %v", bc.ErrInvalidInput, err)
		}
	}
	if raw := options[SettingInstalls]; raw != "" {
		if err := json.Unmarshal([]byte(raw), &s.Installs); err != nil {
			return Settings{}, fmt.Errorf("%w: typescript installs option: %v", bc.ErrInvalidInput, err)
		}
		for _, in := range s.Installs {
			if !filepath.IsAbs(in.Path) || len(in.SHA256) != 64 || (in.Dir != "." && (!fs.ValidPath(in.Dir) || strings.ContainsAny(in.Dir, "\\:"))) {
				return Settings{}, fmt.Errorf("%w: typescript install %q needs a checkout directory, an absolute path and a digest", bc.ErrInvalidInput, in.Dir)
			}
		}
	}
	s.Compiler = options[SettingCompiler]
	s.Normalize()
	return s, nil
}

// ValidateSettings checks the option values a profile carries.
func ValidateSettings(options map[string]string) error {
	for key := range options {
		switch key {
		case SettingProjects, SettingPackages, SettingInstalls, SettingCompiler:
		default:
			return fmt.Errorf("unknown TypeScript setting %q", key)
		}
	}
	_, err := DecodeSettings(options)
	return err
}

// project finds the nearest project governing a checkout-relative path
// that satisfies want; a project recorded without a setting defers to its
// ancestors for that setting, which is how inherited mappings are stored
// once.
func (s Settings) project(file string, want func(*Project) bool) *Project {
	var best *Project
	for i := range s.Projects {
		p := &s.Projects[i]
		if !want(p) || p.Dir != "." && !strings.HasPrefix(file, p.Dir+"/") {
			continue
		}
		if best == nil || len(p.Dir) > len(best.Dir) {
			best = p
		}
	}
	return best
}

func hasPaths(p *Project) bool   { return len(p.Paths) > 0 }
func hasBaseURL(p *Project) bool { return p.BaseURL != "" }

// mapped applies the project's path mappings to a specifier: the longest
// matching pattern wins, and every target of that pattern is a candidate.
func (p *Project) mapped(spec string) ([]string, bool) {
	if p == nil {
		return nil, false
	}
	var best string
	bestLen := -1
	for pattern := range p.Paths {
		prefix, suffix, ok := strings.Cut(pattern, "*")
		if !ok {
			if pattern == spec && len(pattern) > bestLen {
				best, bestLen = pattern, len(pattern)
			}
			continue
		}
		if len(spec) >= len(prefix)+len(suffix) && strings.HasPrefix(spec, prefix) && strings.HasSuffix(spec, suffix) && len(prefix) > bestLen {
			best, bestLen = pattern, len(prefix)
		}
	}
	if bestLen < 0 {
		return nil, false
	}
	prefix, suffix, wild := strings.Cut(best, "*")
	var out []string
	for _, target := range p.Paths[best] {
		if wild {
			matched := spec[len(prefix) : len(spec)-len(suffix)]
			target = strings.Replace(target, "*", matched, 1)
		}
		out = append(out, target)
	}
	return out, true
}
