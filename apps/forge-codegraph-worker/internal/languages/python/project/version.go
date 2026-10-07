package project

import (
	"fmt"
	"slices"
	"strconv"
	"strings"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

// The analyzer targets major/minor profiles, not an installed interpreter.
// Constraints are evaluated over stable numeric releases. Unsupported forms
// fail explicitly rather than approximating PEP 440 with SemVer rules.
type release struct {
	parts [3]int
	count int
}

func parseRelease(raw string) (release, error) {
	var v release
	parts := strings.Split(strings.TrimSpace(raw), ".")
	if len(parts) < 1 || len(parts) > 3 {
		return v, fmt.Errorf("expected a numeric Python release, got %q", raw)
	}
	for i, part := range parts {
		if part == "" || strings.IndexFunc(part, func(r rune) bool { return r < '0' || r > '9' }) >= 0 {
			return v, fmt.Errorf("expected a stable numeric Python release, got %q", raw)
		}
		n, err := strconv.Atoi(part)
		if err != nil || n > 1_000_000 {
			return v, fmt.Errorf("invalid Python release %q", raw)
		}
		v.parts[i] = n
	}
	v.count = len(parts)
	return v, nil
}

func (v release) profile() string { return fmt.Sprintf("%d.%d", v.parts[0], v.parts[1]) }
func (v release) compare(other release) int {
	for i := range v.parts {
		if v.parts[i] < other.parts[i] {
			return -1
		}
		if v.parts[i] > other.parts[i] {
			return 1
		}
	}
	return 0
}

type specifier struct {
	op     string
	v      release
	prefix bool
}

func parseSpecifiers(raw string) ([]specifier, error) {
	if len(raw) > 4096 || strings.Count(raw, ",") >= 64 {
		return nil, fmt.Errorf("requires-python exceeds 4096 bytes or 64 clauses")
	}
	if strings.TrimSpace(raw) == "" {
		return nil, nil
	}
	var result []specifier
	for _, clause := range strings.Split(raw, ",") {
		clause = strings.TrimSpace(clause)
		var op string
		for _, candidate := range []string{"~=", "==", "!=", "<=", ">=", "<", ">"} {
			if strings.HasPrefix(clause, candidate) {
				op = candidate
				break
			}
		}
		if op == "" {
			return nil, fmt.Errorf("unsupported requires-python clause %q", clause)
		}
		value := strings.TrimSpace(strings.TrimPrefix(clause, op))
		prefix := strings.HasSuffix(value, ".*")
		if prefix {
			if op != "==" && op != "!=" {
				return nil, fmt.Errorf("wildcards require == or != in %q", clause)
			}
			value = strings.TrimSuffix(value, ".*")
		}
		v, err := parseRelease(value)
		if err != nil {
			return nil, err
		}
		if op == "~=" && v.count < 2 {
			return nil, fmt.Errorf("compatible release needs at least two components: %q", clause)
		}
		result = append(result, specifier{op, v, prefix})
	}
	return result, nil
}

func (s specifier) accepts(v release) bool {
	cmp := v.compare(s.v)
	if s.prefix {
		match := true
		for i := 0; i < s.v.count; i++ {
			match = match && v.parts[i] == s.v.parts[i]
		}
		if s.op == "!=" {
			return !match
		}
		return match
	}
	switch s.op {
	case "==":
		return cmp == 0
	case "!=":
		return cmp != 0
	case ">=":
		return cmp >= 0
	case "<=":
		return cmp <= 0
	case ">":
		return cmp > 0
	case "<":
		return cmp < 0
	case "~=":
		if cmp < 0 {
			return false
		}
		for i := 0; i < s.v.count-1; i++ {
			if v.parts[i] != s.v.parts[i] {
				return false
			}
		}
		return true
	}
	return false
}

// A minor-only target is compatible when at least one stable patch release
// satisfies every constraint. Testing boundaries and their successors is
// sufficient for these interval/equality predicates; no arbitrary patch cap.
func compatible(v release, specs []specifier) bool {
	candidates := []release{v}
	if v.count < 3 {
		for _, s := range specs {
			if s.v.parts[0] == v.parts[0] && s.v.parts[1] == v.parts[1] {
				for _, patch := range []int{s.v.parts[2], s.v.parts[2] + 1} {
					candidate := v
					candidate.parts[2] = patch
					candidates = append(candidates, candidate)
				}
			}
		}
	}
	for _, candidate := range candidates {
		ok := true
		for _, s := range specs {
			ok = ok && s.accepts(candidate)
		}
		if ok {
			return true
		}
	}
	return false
}

type versionSelection struct{ version, source, request string }

// supportedPythons are the analysis targets, oldest first.
var supportedPythons = []string{"3.10", "3.11", "3.12", "3.13"}

// nearestSupported is the analysis target closest to a release.
func nearestSupported(v release) string {
	switch {
	case v.parts[0] < 3 || (v.parts[0] == 3 && v.parts[1] < 10):
		return supportedPythons[0]
	case v.parts[0] > 3 || v.parts[1] > 13:
		return supportedPythons[len(supportedPythons)-1]
	}
	return v.profile()
}

// discoverVersion picks the Python the analysis targets: the operator's,
// else the project's .python-version, else its analyzer's pythonVersion,
// else the default, kept inside requires-python when that allows it. What
// the project declares but the analysis cannot follow as written (a file
// entry that is not a version, targets that disagree, a version outside the
// supported range) never fails the build: a note says what was used
// instead. Only the worker's own configuration is an error.
func discoverVersion(c Config, options map[string]any, pyrightSource, versionFile, requires string) (versionSelection, []string, error) {
	var notes []string
	note := func(format string, args ...any) { notes = append(notes, fmt.Sprintf(format, args...)) }
	specs, err := parseSpecifiers(requires)
	if err != nil {
		note("requires-python %q could not be read (%v), so it was not applied", requires, err)
		specs = nil
	}
	var choice release
	var source, request string
	if c.VersionExplicit {
		v, err := parseRelease(c.Version)
		if err != nil || v.count < 2 {
			return versionSelection{}, nil, fmt.Errorf("%w: the worker's Python version %q is not major.minor", bc.ErrInvalidInput, c.Version)
		}
		choice, source, request = v, "worker.version", c.Version
	} else {
		var pin *release
		for _, line := range strings.Split(versionFile, "\n") {
			line, _, _ = strings.Cut(line, "#")
			line = strings.TrimSpace(line)
			if line == "" {
				continue
			}
			v, e := parseRelease(line)
			if e != nil || v.count < 2 {
				note(".python-version entry %q is not a numeric major.minor[.patch] release and was passed over", line)
				continue
			}
			if pin != nil {
				note(".python-version lists several runtimes; the first, %s, is analysed", request)
				break
			}
			pin = &v
			choice, source, request = v, ".python-version", line
		}
		if raw, exists := options["pythonVersion"]; exists {
			value, ok := raw.(string)
			v, e := parseRelease(value)
			switch {
			case !ok || e != nil || v.count < 2:
				note("%s %v is not a major.minor Python version and was passed over", pyrightSource, raw)
			case pin != nil && pin.profile() != v.profile():
				// The analyzer's own setting describes the analysis.
				note("%s is %s but .python-version is %s; %s is analysed", pyrightSource, value, request, value)
				choice, source, request = v, pyrightSource, value
			case pin == nil:
				choice, source, request = v, pyrightSource, value
			}
		}
	}
	if source == "" {
		fallback, e := parseRelease(c.Version)
		if e != nil || fallback.count != 2 {
			return versionSelection{}, nil, fmt.Errorf("%w: invalid fallback Python version %q", bc.ErrInvalidInput, c.Version)
		}
		choice, source, request = fallback, "default", c.Version
		if len(specs) > 0 {
			source = "requires-python:compatible-default"
			if !compatible(choice, specs) {
				source = "requires-python:lowest-supported"
				found := false
				for _, version := range supportedPythons {
					candidate, _ := parseRelease(version)
					if compatible(candidate, specs) {
						choice, request, found = candidate, version, true
						break
					}
				}
				if !found {
					// The end of the supported range nearest what it allows.
					target := ""
					for _, probe := range []struct{ version, target string }{{"3.14", "3.13"}, {"3.15", "3.13"}, {"3.9", "3.10"}, {"3.8", "3.10"}, {"3.7", "3.10"}} {
						candidate, _ := parseRelease(probe.version)
						if target == "" && compatible(candidate, specs) {
							target = probe.target
						}
					}
					if target == "" {
						target, source = c.Version, "default"
					} else {
						source = "requires-python:nearest-supported"
					}
					note("requires-python %q allows no Python the analysis supports (%s–%s); Python %s is analysed", requires, supportedPythons[0], supportedPythons[len(supportedPythons)-1], target)
					choice, _ = parseRelease(target)
					request = target
				}
			}
		}
	} else if len(specs) > 0 && !compatible(choice, specs) {
		note("Python %s from %s is outside requires-python %q; it is analysed as set", request, source, requires)
	}
	profile := choice.profile()
	if !slices.Contains(supportedPythons, profile) {
		profile = nearestSupported(choice)
		note("Python %s from %s is outside what the analysis supports (%s–%s); it is analysed as Python %s", request, source, supportedPythons[0], supportedPythons[len(supportedPythons)-1], profile)
	}
	return versionSelection{profile, source, request}, notes, nil
}
