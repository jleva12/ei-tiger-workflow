package discover

import (
	"bytes"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"regexp"
	"strings"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

// A bounded XML tree keeps arbitrary plugin configuration available without
// pretending that encoding/xml's last-value-wins decoding is a Maven model.
type element struct {
	name, text string
	children   []*element
}

func (e *element) all(name string) []*element {
	var out []*element
	if e != nil {
		for _, c := range e.children {
			if c.name == name {
				out = append(out, c)
			}
		}
	}
	return out
}
func (e *element) one(name string) *element {
	if e != nil {
		for _, c := range e.children {
			if c.name == name {
				return c
			}
		}
	}
	return nil
}
func (e *element) value(name string) string {
	if c := e.one(name); c != nil {
		return strings.TrimSpace(c.text)
	}
	return ""
}

func (s *scan) xml(name string) (*element, error) {
	data, err := s.read(name)
	if err != nil {
		return nil, err
	}
	d := xml.NewDecoder(bytes.NewReader(data))
	var root *element
	var stack []*element
	var nodes uint64
	for {
		if err := s.ctx.Err(); err != nil {
			return nil, err
		}
		tok, err := d.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("%w: %s: %v", bc.ErrInvalidInput, name, err)
		}
		switch t := tok.(type) {
		case xml.StartElement:
			nodes++
			if nodes > s.r.Limits.MaxRecords || uint32(len(stack)+1) > s.r.Limits.MaxDepth {
				return nil, bc.ErrLimitExceeded
			}
			e := &element{name: t.Name.Local}
			if len(stack) == 0 {
				if root != nil {
					return nil, fmt.Errorf("%w: multiple XML roots", bc.ErrInvalidInput)
				}
				root = e
			} else {
				p := stack[len(stack)-1]
				p.children = append(p.children, e)
			}
			stack = append(stack, e)
		case xml.EndElement:
			stack = stack[:len(stack)-1]
		case xml.CharData:
			if len(stack) > 0 {
				stack[len(stack)-1].text += string(t)
			} else if strings.TrimSpace(string(t)) != "" {
				return nil, fmt.Errorf("%w: trailing XML text", bc.ErrInvalidInput)
			}
		case xml.Directive:
			return nil, fmt.Errorf("%w: XML directives/DTDs are unsupported", bc.ErrInvalidInput)
		}
	}
	if root == nil || root.name != "project" {
		return nil, fmt.Errorf("%w: expected Maven project in %s", bc.ErrInvalidInput, name)
	}
	return root, nil
}

var propertyRE = regexp.MustCompile(`\$\{([^{}]+)\}`)

func interpolate(raw string, props map[string]string) string {
	// Cycles and excessive expansion remain unresolved rather than looping or
	// allocating an exponential property substitution.
	seen := map[string]bool{}
	for i := 0; i < 32 && len(raw) <= 4096; i++ {
		if seen[raw] {
			return raw
		}
		seen[raw] = true
		next := propertyRE.ReplaceAllStringFunc(raw, func(token string) string {
			key := token[2 : len(token)-1]
			if v, ok := props[key]; ok && len(v) <= 4096 {
				return v
			}
			return token
		})
		if len(next) > 4096 {
			return "${oversized-property}"
		}
		if next == raw {
			return raw
		}
		raw = next
	}
	return raw
}

type pomModel struct {
	file                                string
	node                                *element
	props                               map[string]string
	group, artifact, version, packaging string
	source, testSource                  string
	compiler                            map[string]string
	compilerInherited                   bool
	deps, managed                       []*element
	// These are raw declarations, re-interpolated in each child's environment.
	plugins []*element
	parent  *pomModel
}

type mavenReader struct {
	s        *scan
	models   map[string]*pomModel
	visiting map[string]bool
	visited  map[string]bool
}

func (s *scan) maven() error {
	r := &mavenReader{s: s, models: map[string]*pomModel{}, visiting: map[string]bool{}, visited: map[string]bool{}}
	if s.markers["pom.xml"] {
		return r.module("pom.xml", 0)
	}
	found := false
	for _, file := range sortedKeys(s.markers) {
		if path.Base(file) != "pom.xml" || buildFixturePath(file) {
			continue
		}
		found = true
		if err := r.module(file, 0); err != nil {
			return err
		}
	}
	if !found {
		return fmt.Errorf("%w: no Maven pom.xml found", bc.ErrInvalidInput)
	}
	return nil
}

// Build files embedded in source/resources/build output are data, not roots of
// independent builds. Explicit reactor/settings references can still use them.
func buildFixturePath(name string) bool {
	for _, p := range strings.Split(path.Dir(name), "/") {
		switch p {
		case "src", "target", "build", "testdata":
			return true
		}
	}
	return false
}

func (r *mavenReader) load(file string, depth uint32) (*pomModel, error) {
	if depth > r.s.r.Limits.MaxDepth {
		return nil, bc.ErrLimitExceeded
	}
	if r.visiting[file] {
		return nil, fmt.Errorf("%w: Maven parent cycle at %s", bc.ErrInvalidInput, file)
	}
	if m := r.models[file]; m != nil {
		return m, nil
	}
	r.visiting[file] = true
	defer delete(r.visiting, file)
	n, err := r.s.xml(file)
	if err != nil {
		return nil, err
	}
	for _, key := range []string{"parent", "groupId", "artifactId", "version", "packaging", "properties", "build", "dependencies", "dependencyManagement", "modules"} {
		if len(n.all(key)) > 1 {
			return nil, fmt.Errorf("%w: duplicate Maven %s in %s", bc.ErrInvalidInput, key, file)
		}
	}
	if v := n.value("modelVersion"); v != "" && v != "4.0.0" {
		r.s.gap(file+"/model", "Maven model version", "Maven model version differs from the supported 4.0.0 declarations", "", "")
	}
	m := &pomModel{file: file, node: n, props: map[string]string{}, compiler: map[string]string{}, compilerInherited: true}
	if p := n.one("parent"); p != nil {
		rel := "../pom.xml"
		if p.one("relativePath") != nil {
			rel = p.value("relativePath")
		}
		parentFile, ok := sourcePath(path.Dir(file), rel)
		if ok && r.s.dirs[parentFile] {
			parentFile = path.Join(parentFile, "pom.xml")
		}
		if ok {
			parent, loadErr := r.load(parentFile, depth+1)
			if loadErr != nil && !errors.Is(loadErr, os.ErrNotExist) {
				return nil, loadErr
			}
			if loadErr == nil && p.value("groupId") == parent.group && p.value("artifactId") == parent.artifact && p.value("version") == parent.version {
				m.parent = parent
			}
		}
		if m.parent == nil {
			r.s.gap(file+"/parent", "Maven parent POM", "External or mismatched Maven parent is unavailable; inherited settings and dependency management are incomplete", "", "")
		}
	}
	if p := m.parent; p != nil {
		for k, v := range p.props {
			m.props[k] = v
		}
		if p.compilerInherited {
			for k, v := range p.compiler {
				m.compiler[k] = v
			}
		}
		m.group, m.version = p.group, p.version
		m.source, m.testSource = p.source, p.testSource
		m.deps = append(m.deps, p.deps...)
		m.managed = append(m.managed, p.managed...)
		for _, plugin := range p.plugins {
			if plugin.value("inherited") != "false" {
				m.plugins = append(m.plugins, plugin)
			}
		}
	}
	if p := n.one("parent"); p != nil {
		if m.group == "" {
			m.group = p.value("groupId")
		}
		if m.version == "" {
			m.version = p.value("version")
		}
	}
	if props := n.one("properties"); props != nil {
		for _, p := range props.children {
			m.props[p.name] = strings.TrimSpace(p.text)
		}
	}
	if v := n.value("groupId"); v != "" {
		m.group = v
	}
	if v := n.value("version"); v != "" {
		m.version = v
	}
	m.artifact = n.value("artifactId")
	m.packaging = n.value("packaging")
	if m.packaging == "" {
		m.packaging = "jar"
	}
	// '.' is deliberately module-local, so inherited ${basedir} roots rebase to
	// the child instead of retaining machine-specific parent checkout paths.
	m.props["basedir"], m.props["project.basedir"] = ".", "."
	for _, prefix := range []string{"project.", "pom."} {
		m.props[prefix+"groupId"] = m.group
		m.props[prefix+"artifactId"] = m.artifact
		m.props[prefix+"version"] = m.version
	}
	m.group = interpolate(m.group, m.props)
	m.artifact = interpolate(m.artifact, m.props)
	m.version = interpolate(m.version, m.props)
	build := n.one("build")
	if v := build.value("sourceDirectory"); v != "" {
		m.source = v
	}
	if v := build.value("testSourceDirectory"); v != "" {
		m.testSource = v
	}
	buildDir := build.value("directory")
	if buildDir == "" {
		buildDir = "target"
	}
	m.props["project.build.directory"] = buildDir
	plugins := append([]*element{}, build.one("pluginManagement").one("plugins").all("plugin")...)
	plugins = append(plugins, build.one("plugins").all("plugin")...)
	for _, plugin := range plugins {
		if plugin.value("artifactId") == "maven-compiler-plugin" && (plugin.value("groupId") == "" || plugin.value("groupId") == "org.apache.maven.plugins") {
			if v := plugin.value("inherited"); v != "" {
				m.compilerInherited = v != "false"
			}
			for _, c := range plugin.one("configuration").childrenOrNil() {
				m.compiler[c.name] = strings.TrimSpace(c.text)
			}
			for _, ex := range plugin.one("executions").all("execution") {
				if ex.value("id") == "default-compile" || ex.value("id") == "default-testCompile" {
					prefix := "main."
					if ex.value("id") == "default-testCompile" {
						prefix = "test."
					}
					for _, c := range ex.one("configuration").childrenOrNil() {
						m.compiler[prefix+c.name] = strings.TrimSpace(c.text)
					}
				} else {
					r.s.gap(file+"/compiler-executions", "Maven compiler executions", "Additional compiler executions can define other compilation environments and have not been evaluated", "", "")
				}
			}
		}
	}
	// Only applied plugins add roots; pluginManagement alone does not execute.
	m.plugins = append(m.plugins, build.one("plugins").all("plugin")...)
	m.deps = mergeDependencies(m.deps, n.one("dependencies").all("dependency"))
	m.managed = mergeDependencies(m.managed, n.one("dependencyManagement").one("dependencies").all("dependency"))
	if len(n.one("profiles").all("profile")) > 0 {
		r.s.gap(file+"/profiles", "Maven profiles", "Maven profile activation was not evaluated; profile-dependent modules, sources, releases and dependencies may differ", "", "")
	}
	r.models[file] = m
	return m, nil
}

func (e *element) childrenOrNil() []*element {
	if e == nil {
		return nil
	}
	return e.children
}
func dependencyKey(e *element) string {
	typ := e.value("type")
	if typ == "" {
		typ = "jar"
	}
	return e.value("groupId") + ":" + e.value("artifactId") + ":" + typ + ":" + e.value("classifier")
}
func mergeDependencies(parent, child []*element) []*element {
	out := append([]*element{}, parent...)
	for _, c := range child {
		found := false
		for i, p := range out {
			if dependencyKey(c) == dependencyKey(p) {
				out[i] = c
				found = true
				break
			}
		}
		if !found {
			out = append(out, c)
		}
	}
	return out
}

func (m *pomModel) release(test bool) string {
	keys := []string{"main.release"}
	if test {
		keys = []string{"test.release", "testRelease"}
	}
	keys = append(keys, "release")
	for _, k := range keys {
		if v := interpolate(m.compiler[k], m.props); v != "" {
			return v
		}
		if v := interpolate(m.props["maven.compiler."+k], m.props); v != "" {
			return v
		}
	}
	if !test {
		if v := interpolate(m.compiler["main.source"], m.props); v != "" {
			return v
		}
	}
	if test {
		for _, k := range []string{"test.source", "testSource"} {
			if v := interpolate(m.compiler[k], m.props); v != "" {
				return v
			}
			if v := interpolate(m.props["maven.compiler."+k], m.props); v != "" {
				return v
			}
		}
	}
	if v := interpolate(m.compiler["source"], m.props); v != "" {
		return v
	}
	return interpolate(m.props["maven.compiler.source"], m.props)
}

func (r *mavenReader) module(file string, depth uint32) error {
	if depth > r.s.r.Limits.MaxDepth {
		return bc.ErrLimitExceeded
	}
	if r.visited[file] {
		return nil
	}
	r.visited[file] = true
	if err := r.s.budget(); err != nil {
		return err
	}
	m, err := r.load(file, 0)
	if err != nil {
		return err
	}
	dir := path.Dir(file)
	id := bc.ModuleID(stable("maven", dir))
	module := bc.Module{ID: id, Name: m.artifact, Directory: dir}
	if module.Name == "" || strings.Contains(module.Name, "${") {
		module.Name = "Maven module " + dir
	}
	if literal(m.group) && literal(m.artifact) && literal(m.version) {
		module.Coordinates = &bc.Coordinates{Group: m.group, Name: m.artifact, Version: m.version, Extension: m.packaging}
	}
	r.s.in.Modules = append(r.s.in.Modules, module)
	if m.packaging != "pom" {
		var sets []int
		for _, test := range []bool{false, true} {
			name, raw, kind := "main", m.source, bc.SourceSetMain
			if test {
				name, raw, kind = "test", m.testSource, bc.SourceSetTest
			}
			explicit := raw != ""
			if raw == "" {
				raw = "src/" + name + "/java"
			}
			root, ok := sourcePath(dir, interpolate(raw, m.props))
			if !ok {
				r.s.gap(file+"/"+name+"/root", "Maven source directory", "Source directory could not be resolved inside the checkout; conventional root is retained only as a discovery candidate", id, "")
				root = path.Join(dir, "src", name, "java")
			}
			roots := []string{root}
			for _, plugin := range m.plugins {
				if plugin.value("groupId") != "org.codehaus.mojo" || plugin.value("artifactId") != "build-helper-maven-plugin" {
					continue
				}
				for _, ex := range plugin.one("executions").all("execution") {
					for _, goal := range ex.one("goals").all("goal") {
						want := "add-source"
						if test {
							want = "add-test-source"
						}
						if strings.TrimSpace(goal.text) != want {
							continue
						}
						config := ex.one("configuration")
						if config == nil {
							config = plugin.one("configuration")
						}
						for _, src := range config.one("sources").all("source") {
							p, ok := sourcePath(dir, interpolate(strings.TrimSpace(src.text), m.props))
							if ok {
								roots = append(roots, p)
							} else {
								r.s.gap(file+"/helper-roots", "build-helper source roots", "A build-helper source path could not be resolved inside the checkout", id, "")
							}
						}
					}
				}
			}
			if test && !explicit && len(roots) == 1 && !r.s.dirs[root] {
				continue
			}
			i := r.s.addSet(id, name, kind, m.release(test), roots)
			sets = append(sets, i)
			set := &r.s.in.SourceSets[i]
			if interpolate(m.compiler["enablePreview"], m.props) == "true" {
				set.EnablePreview = true
			}
			if test && len(sets) > 1 {
				set.Classpath = append([]bc.PathEntry{{Kind: bc.EntrySourceSet, RefID: string(r.s.in.SourceSets[sets[0]].ID)}}, set.Classpath...)
			}
			if err := r.dependencies(m, i, test); err != nil {
				return err
			}
		}
	}
	for _, mod := range m.node.one("modules").all("module") {
		raw := interpolate(strings.TrimSpace(mod.text), m.props)
		child, ok := sourcePath(dir, raw)
		if !ok {
			r.s.gap(file+"/module/"+raw, "Maven reactor module", "A reactor module path could not be resolved inside the checkout", id, "")
			continue
		}
		if !strings.HasSuffix(child, ".xml") {
			child = path.Join(child, "pom.xml")
		}
		if err := r.module(child, depth+1); err != nil {
			if errors.Is(err, os.ErrNotExist) {
				r.s.gap(child, "Maven reactor module", "Declared reactor module POM is missing", id, "")
				continue
			}
			return err
		}
	}
	return r.s.budget()
}

func literal(v string) bool {
	return v != "" && len(v) <= 4096 && !strings.ContainsAny(v, "$\n\r\t") && strings.TrimSpace(v) == v
}

func (r *mavenReader) dependencies(m *pomModel, setIndex int, test bool) error {
	set := r.s.in.SourceSets[setIndex]
	managed := map[string]*element{}
	for _, d := range m.managed {
		managed[interpolate(dependencyKey(d), m.props)] = d
		if d.value("scope") == "import" {
			r.s.gap(m.file+"/bom/"+dependencyKey(d), "Maven BOM import", "Imported BOM dependency management was not resolved", set.ModuleID, "")
		}
	}
	for _, d := range m.deps {
		if err := r.s.budget(); err != nil {
			return err
		}
		key := interpolate(dependencyKey(d), m.props)
		value := func(name string) string {
			v := d.value(name)
			if v == "" {
				v = managed[key].value(name)
			}
			return interpolate(v, m.props)
		}
		scope := value("scope")
		if scope == "" {
			scope = "compile"
		}
		if !test && scope != "compile" && scope != "provided" && scope != "system" {
			continue
		}
		c := bc.Coordinates{Group: value("groupId"), Name: value("artifactId"), Version: value("version"), Classifier: value("classifier"), Extension: value("type")}
		if c.Extension == "" {
			c.Extension = "jar"
		}
		if c.Extension == "test-jar" {
			c.Extension = "jar"
			if c.Classifier == "" {
				c.Classifier = "tests"
			}
		}
		requested := c.Group + ":" + c.Name + ":" + c.Version
		r.s.gap(string(set.ID)+"/dependency/"+key, "Declared dependency "+requested, "Declared Maven dependency (scope "+scope+") has not been resolved into the effective compiler path", set.ModuleID, set.ID)
		if !literal(c.Group) || !literal(c.Name) || !literal(c.Version) || strings.ContainsAny(c.Version, "[](),+") || !literal(c.Extension) || (c.Classifier != "" && !literal(c.Classifier)) {
			continue
		}
		r.s.artifact(c)
	}
	return nil
}

func (s *scan) artifact(c bc.Coordinates) {
	key := c.Group + ":" + c.Name + ":" + c.Version + ":" + c.Extension + ":" + c.Classifier
	id := bc.ArtifactID(stable("artifact", key))
	input := bc.InputID(id)
	if s.seenInputs[input] {
		return
	}
	s.seenInputs[input] = true
	s.in.Inputs = append(s.in.Inputs, bc.Input{ID: input, Kind: bc.InputJAR, UnavailableReason: "Declared artifact bytes were not resolved or downloaded"})
	s.checks = append(s.checks, bc.InputCheck{InputID: input, Status: bc.Missing})
	s.in.Artifacts = append(s.in.Artifacts, bc.Artifact{ID: id, Coordinates: c, BinaryInputID: input})
}
