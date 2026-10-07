package buildcontext

import (
	"encoding/hex"
	"errors"
	"fmt"
	"path"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

type ValidationError struct{ Path, Message string }

func (e ValidationError) Error() string { return e.Path + ": " + e.Message }

func validText(s string) bool {
	if s == "" || len(s) > 4096 || strings.TrimSpace(s) != s || !utf8.ValidString(s) {
		return false
	}
	for _, r := range s {
		if unicode.IsControl(r) {
			return false
		}
	}
	return true
}
func validID(s string) bool { return validText(s) && len(s) <= 512 }

// validOptionValue bounds a language option: structured adapter data such
// as a monorepo's path mappings (hundreds of tsconfig projects), larger than
// a name but still one line.
func validOptionValue(s string) bool {
	if s == "" || len(s) > 4<<20 || strings.TrimSpace(s) != s || !utf8.ValidString(s) {
		return false
	}
	for _, r := range s {
		if unicode.IsControl(r) {
			return false
		}
	}
	return true
}
func validDigest(s string) bool {
	b, err := hex.DecodeString(s)
	return err == nil && len(b) == 32 && s == strings.ToLower(s)
}
func ValidPath(s string) bool {
	return validText(s) && !path.IsAbs(s) && path.Clean(s) == s && s != ".." && !strings.HasPrefix(s, "../") && !strings.ContainsAny(s, "\\:")
}
func ValidRoot(s string) bool {
	if len(s) == 0 || len(s) > 128 {
		return false
	}
	for _, r := range s {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '_' || r == '-') {
			return false
		}
	}
	return true
}

type validator struct{ errs []error }

func (v *validator) require(ok bool, p, message string) {
	if !ok {
		v.errs = append(v.errs, ValidationError{p, message})
	}
}
func (v *validator) text(p, s string) {
	v.require(validText(s), p, "must be nonempty valid UTF-8 without boundary whitespace/control characters")
}
func (v *validator) ref(table map[string]bool, p, id string) {
	v.require(table[id], p, "unknown or missing reference "+id)
}
func (v *validator) ids(ids []string, p string) map[string]bool {
	seen := map[string]bool{}
	for i, id := range ids {
		q := fmt.Sprintf("%s[%d].id", p, i)
		v.require(validID(id), q, "invalid ID")
		v.require(!seen[id], q, "duplicate ID")
		seen[id] = true
	}
	return seen
}
func (v *validator) coordinates(c Coordinates, p string) {
	v.text(p+".group", c.Group)
	v.text(p+".name", c.Name)
	v.text(p+".version", c.Version)
	v.text(p+".extension", c.Extension)
	if c.Classifier != "" {
		v.text(p+".classifier", c.Classifier)
	}
}
func ids[T any](items []T, id func(T) string) []string {
	out := make([]string, len(items))
	for i, x := range items {
		out[i] = id(x)
	}
	return out
}

// Validate is structural and performs no I/O. It does not certify declared JDK
// metadata, dependency mediation, Java readability, or completeness of an export.
func (in Inventory) Validate() error {
	v := &validator{}
	inputs := v.ids(ids(in.Inputs, func(x Input) string { return string(x.ID) }), "inputs")
	jdks := v.ids(ids(in.JDKs, func(x JDK) string { return string(x.ID) }), "jdks")
	modules := v.ids(ids(in.Modules, func(x Module) string { return string(x.ID) }), "modules")
	sets := v.ids(ids(in.SourceSets, func(x SourceSet) string { return string(x.ID) }), "source_sets")
	artifacts := v.ids(ids(in.Artifacts, func(x Artifact) string { return string(x.ID) }), "artifacts")
	gaps := v.ids(ids(in.MissingInputs, func(x MissingInput) string { return string(x.ID) }), "missing_inputs")
	v.require(len(modules) > 0, "modules", "at least one module is required")
	v.require(len(sets) > 0, "source_sets", "at least one source set is required")
	inputByID := map[InputID]Input{}
	for i, x := range in.Inputs {
		p := fmt.Sprintf("inputs[%d]", i)
		inputByID[x.ID] = x
		v.require(x.Kind == InputSourceRoot || x.Kind == InputGeneratedRoot || x.Kind == InputJAR || x.Kind == InputClasses || x.Kind == InputJDK, p+".kind", "unsupported input kind")
		v.require((x.Location != nil) != (x.UnavailableReason != ""), p, "exactly one location or unavailable reason is required")
		if x.UnavailableReason != "" {
			v.text(p+".unavailable_reason", x.UnavailableReason)
		}
		if x.Location != nil {
			v.require(ValidRoot(x.Location.Root), p+".location.root", "invalid logical root")
			v.require(ValidPath(x.Location.Path), p+".location.path", "normalized relative slash path required")
			if x.Kind == InputSourceRoot {
				v.require(x.Location.Root == "checkout", p+".location.root", "ordinary source roots must belong to checkout")
			}
		}
		if x.Kind == InputSourceRoot {
			v.require(x.SHA256 == "", p+".sha256", "ordinary source roots are pinned by the checkout snapshot")
		} else if x.Location != nil || x.SHA256 != "" {
			v.require(validDigest(x.SHA256), p+".sha256", "lowercase SHA-256 required")
		}
	}
	inputRef := func(id InputID, p string, kinds ...InputKind) {
		v.ref(inputs, p, string(id))
		if x, ok := inputByID[id]; ok {
			match := false
			for _, k := range kinds {
				match = match || x.Kind == k
			}
			v.require(match, p, "input kind does not match role")
		}
	}
	jdkByID := map[JDKID]JDK{}
	for i, x := range in.JDKs {
		p := fmt.Sprintf("jdks[%d]", i)
		jdkByID[x.ID] = x
		inputRef(x.HomeInputID, p+".home_input_id", InputJDK)
		v.text(p+".vendor", x.Vendor)
		v.text(p+".version", x.Version)
		v.require(x.Major >= 8 && x.Major <= 999, p+".major", "JDK major must be between 8 and 999")
	}
	for i, x := range in.Modules {
		p := fmt.Sprintf("modules[%d]", i)
		v.text(p+".name", x.Name)
		v.require(ValidPath(x.Directory), p+".directory", "normalized checkout-relative directory required")
		if x.JavaModuleName != "" {
			v.text(p+".java_module_name", x.JavaModuleName)
		}
		if x.Coordinates != nil {
			v.coordinates(*x.Coordinates, p+".coordinates")
		}
	}
	setByID := map[SourceSetID]SourceSet{}
	for _, x := range in.SourceSets {
		setByID[x.ID] = x
	}
	graph := map[string][]string{}
	for i, x := range in.SourceSets {
		p := fmt.Sprintf("source_sets[%d]", i)
		v.ref(modules, p+".module_id", string(x.ModuleID))
		v.text(p+".name", x.Name)
		v.require(x.Kind == SourceSetMain || x.Kind == SourceSetTest || x.Kind == SourceSetCustom, p+".kind", "unsupported source-set kind")
		language, version := x.SyntaxLanguage()
		v.text(p+".language", language)
		v.require(strings.ToLower(language) == language, p+".language", "lowercase language ID required")
		v.text(p+".language_version", version)
		if language == "java" {
			v.ref(jdks, p+".jdk_id", string(x.JDKID))
			v.require(x.TargetRelease >= 8 && x.TargetRelease <= jdkByID[x.JDKID].Major, p+".target_release", "target release must be at least 8 and no newer than selected JDK")
			v.require(version == strconv.Itoa(x.TargetRelease), p+".language_version", "Java language version must match target release")
			v.require(!x.EnablePreview || x.TargetRelease == jdkByID[x.JDKID].Major, p+".enable_preview", "preview requires target release equal to JDK major")
		} else {
			v.require(x.JDKID == "" && x.TargetRelease == 0 && !x.EnablePreview && x.OutputInputID == "" && len(x.Classpath) == 0 && len(x.ModulePath) == 0, p, "non-Java source sets must not contain Java build settings")
		}
		for key, value := range x.LanguageOptions {
			v.text(p+".language_options.key", key)
			v.require(validOptionValue(value), p+".language_options."+key, "must be nonempty valid UTF-8 of at most 4 MiB without boundary whitespace/control characters")
		}
		for _, pattern := range append(append([]string(nil), x.IncludePatterns...), x.ExcludePatterns...) {
			v.require(ValidSourcePattern(pattern), p+".source_patterns", "relative source glob required")
		}
		v.require(len(x.SourceRootIDs)+len(x.GeneratedRootIDs) > 0, p, "at least one source or generated root required")
		for j, id := range x.SourceRootIDs {
			inputRef(id, fmt.Sprintf("%s.source_root_ids[%d]", p, j), InputSourceRoot)
		}
		for j, id := range x.GeneratedRootIDs {
			inputRef(id, fmt.Sprintf("%s.generated_root_ids[%d]", p, j), InputGeneratedRoot)
		}
		if x.OutputInputID != "" {
			inputRef(x.OutputInputID, p+".output_input_id", InputClasses)
		}
		for _, list := range []struct {
			name    string
			entries []PathEntry
		}{{"classpath", x.Classpath}, {"module_path", x.ModulePath}} {
			for j, e := range list.entries {
				q := fmt.Sprintf("%s.%s[%d]", p, list.name, j)
				switch e.Kind {
				case EntryArtifact:
					v.ref(artifacts, q+".ref_id", e.RefID)
				case EntrySourceSet:
					v.ref(sets, q+".ref_id", e.RefID)
					v.require(e.RefID != string(x.ID), q, "source set cannot depend on itself")
					graph[string(x.ID)] = append(graph[string(x.ID)], e.RefID)
				case EntryMissing:
					v.ref(gaps, q+".ref_id", e.RefID)
				default:
					v.require(false, q+".kind", "unsupported path entry kind")
				}
			}
		}
	}
	// Iterative Kahn traversal keeps validation bounded even for deep chains.
	indegree := map[string]int{}
	for id := range sets {
		indegree[id] = 0
	}
	for _, deps := range graph {
		for _, id := range deps {
			if sets[id] {
				indegree[id]++
			}
		}
	}
	var ready []string
	for id, n := range indegree {
		if n == 0 {
			ready = append(ready, id)
		}
	}
	visited := 0
	for len(ready) > 0 {
		id := ready[len(ready)-1]
		ready = ready[:len(ready)-1]
		visited++
		for _, dep := range graph[id] {
			if !sets[dep] {
				continue
			}
			indegree[dep]--
			if indegree[dep] == 0 {
				ready = append(ready, dep)
			}
		}
	}
	v.require(visited == len(sets), "source_sets", "cyclic source-set dependency")
	for i, x := range in.Artifacts {
		p := fmt.Sprintf("artifacts[%d]", i)
		v.coordinates(x.Coordinates, p+".coordinates")
		inputRef(x.BinaryInputID, p+".binary_input_id", InputJAR, InputClasses)
		if x.SourcesInputID != "" {
			inputRef(x.SourcesInputID, p+".sources_input_id", InputJAR)
		}
		if x.Origin != nil {
			v.text(p+".origin.repository_id", x.Origin.RepositoryID)
			v.text(p+".origin.snapshot_id", x.Origin.SnapshotID)
			if x.Origin.ModuleID != "" {
				v.require(validID(x.Origin.ModuleID), p+".origin.module_id", "invalid source module ID")
			}
			if x.Origin.SourceSetID != "" {
				v.require(validID(x.Origin.SourceSetID), p+".origin.source_set_id", "invalid source-set ID")
				v.require(x.Origin.ModuleID != "", p+".origin.module_id", "source-set mapping requires module ID")
			}
		}
	}
	for i, x := range in.MissingInputs {
		p := fmt.Sprintf("missing_inputs[%d]", i)
		v.text(p+".requested", x.Requested)
		v.text(p+".reason", x.Reason)
		if x.ModuleID != "" {
			v.ref(modules, p+".module_id", string(x.ModuleID))
		}
		if x.SourceSetID != "" {
			v.ref(sets, p+".source_set_id", string(x.SourceSetID))
			if x.ModuleID != "" {
				v.require(setByID[x.SourceSetID].ModuleID == x.ModuleID, p, "source-set/module mismatch")
			}
		}
	}
	if len(v.errs) > 0 {
		return fmt.Errorf("%w: %w", ErrInvalidInput, errors.Join(v.errs...))
	}
	return nil
}

func (in Inventory) RecordCount() uint64 {
	n := uint64(len(in.Inputs) + len(in.JDKs) + len(in.Modules) + len(in.SourceSets) + len(in.Artifacts) + len(in.MissingInputs))
	for _, s := range in.SourceSets {
		n += uint64(len(s.SourceRootIDs) + len(s.GeneratedRootIDs) + len(s.Classpath) + len(s.ModulePath) + len(s.LanguageOptions) + len(s.IncludePatterns) + len(s.ExcludePatterns))
	}
	return n
}
