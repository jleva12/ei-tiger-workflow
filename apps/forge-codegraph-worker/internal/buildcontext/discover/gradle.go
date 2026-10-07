package discover

import (
	"fmt"
	"path"
	"regexp"
	"strings"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

func isGradleMarker(name string) bool {
	switch name {
	case "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts":
		return true
	}
	return false
}
func (s *scan) hasGradle(dir string) bool {
	for _, name := range []string{"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"} {
		if s.markers[path.Join(dir, name)] {
			return true
		}
	}
	return false
}
func (s *scan) gradleFile(dir, base string) (string, error) {
	g, k := path.Join(dir, base+".gradle"), path.Join(dir, base+".gradle.kts")
	if s.markers[g] && s.markers[k] {
		return "", fmt.Errorf("%w: both %s and %s exist", bc.ErrInvalidInput, g, k)
	}
	if s.markers[g] {
		return g, nil
	}
	if s.markers[k] {
		return k, nil
	}
	return "", nil
}

type gradleSet struct {
	name     string
	roots    []string
	release  string
	declared bool
}
type gradleModel struct {
	dir, name                  string
	props                      map[string]string
	release, source, toolchain string
	sets                       map[string]*gradleSet
	dependencies               []statement
	preview                    bool
}

func (s *scan) gradle() error {
	roots := []string{}
	if s.hasGradle(".") {
		roots = append(roots, ".")
	} else {
		for _, file := range sortedKeys(s.markers) {
			if !isGradleMarker(path.Base(file)) || buildFixturePath(file) {
				continue
			}
			dir := path.Dir(file)
			covered := false
			for _, root := range roots {
				if dir == root || strings.HasPrefix(dir, root+"/") {
					covered = true
				}
			}
			if !covered {
				roots = append(roots, dir)
			}
		}
	}
	if len(roots) == 0 {
		return fmt.Errorf("%w: no Gradle build found", bc.ErrInvalidInput)
	}
	for _, root := range roots {
		if err := s.gradleBuild(root); err != nil {
			return err
		}
	}
	return nil
}

func (s *scan) gradleBuild(root string) error {
	settings, err := s.gradleFile(root, "settings")
	if err != nil {
		return err
	}
	projects := map[string]string{":": root}
	rootName := "Gradle project " + root
	if settings != "" {
		stmts, err := s.gradleStatements(settings)
		if err != nil {
			return err
		}
		// Includes are read first so projectDir assignments can appear anywhere.
		for _, st := range stmts {
			if first(st) == "include" {
				args, ok := literalArgs(st.head[1:])
				if !ok {
					s.gap(settings+"/includes", "Gradle included projects", "Dynamic project includes could not be enumerated", "", "")
					continue
				}
				for _, name := range args {
					name = ":" + strings.TrimPrefix(name, ":")
					parts := strings.Split(strings.TrimPrefix(name, ":"), ":")
					for i := range parts {
						id := ":" + strings.Join(parts[:i+1], ":")
						dir, ok := sourcePath(root, strings.Join(parts[:i+1], "/"))
						if !ok {
							s.gap(settings+"/include/"+id, "Gradle included project", "Included project directory is outside the checkout or unresolved", "", "")
							continue
						}
						projects[id] = dir
					}
				}
			}
			if first(st) == "includeBuild" {
				s.gap(settings+"/composites", "Gradle composite builds", "Included builds and dependency substitutions are not evaluated", "", "")
			}
			if strings.HasPrefix(compact(st.head), "rootProject.name=") {
				v, ok := literalArgs(st.head[4:])
				if ok && len(v) == 1 {
					rootName = v[0]
				}
			}
		}
		for _, st := range stmts {
			head := compact(st.head)
			if first(st) == "project" && strings.Contains(head, ".projectDir=") {
				args := stringsIn(st.head)
				if len(args) == 2 && (strings.HasSuffix(head, "=file("+args[1]+")") || strings.HasSuffix(head, "=File(rootDir,"+args[1]+")")) {
					id := ":" + strings.TrimPrefix(args[0], ":")
					dir, ok := sourcePath(root, args[1])
					if ok {
						if _, exists := projects[id]; exists {
							projects[id] = dir
							continue
						}
					}
				}
				s.gap(settings+"/project-dirs", "Gradle project directory mapping", "A projectDir expression could not be resolved; default project locations remain candidates", "", "")
			}
		}
	}
	rootFile, err := s.gradleFile(root, "build")
	if err != nil {
		return err
	}
	var rootStmts []statement
	if rootFile != "" {
		rootStmts, err = s.gradleStatements(rootFile)
		if err != nil {
			return err
		}
	}
	rootProps, err := s.properties(path.Join(root, "gradle.properties"))
	if err != nil {
		return err
	}
	// Only unconditional, explicit allprojects/subprojects blocks are inherited.
	var all, sub []statement
	for _, st := range rootStmts {
		if compact(st.head) == "allprojects" {
			all = append(all, st.body...)
		}
		if compact(st.head) == "subprojects" {
			sub = append(sub, st.body...)
		}
	}
	for _, project := range sortedKeys(projects) {
		if err := s.budget(); err != nil {
			return err
		}
		dir := projects[project]
		if !s.dirs[dir] {
			s.gap(root+project+"/directory", "Gradle project directory", "Declared project directory is missing", "", "")
			continue
		}
		file, err := s.gradleFile(dir, "build")
		if err != nil {
			return err
		}
		stmts := rootStmts
		if project != ":" {
			stmts = nil
			if file != "" {
				stmts, err = s.gradleStatements(file)
				if err != nil {
					return err
				}
			}
		}
		props := map[string]string{}
		for k, v := range rootProps {
			props[k] = v
		}
		local, err := s.properties(path.Join(dir, "gradle.properties"))
		if err != nil {
			return err
		}
		for k, v := range local {
			props[k] = v
		}
		m := &gradleModel{dir: dir, name: project, props: props, sets: map[string]*gradleSet{}}
		if project == ":" {
			m.name = rootName
		}
		m.consume(all, "")
		if project != ":" {
			m.consume(sub, "")
			for _, st := range rootStmts {
				args := stringsIn(st.head)
				if first(st) == "project" && len(args) == 1 && ":"+strings.TrimPrefix(args[0], ":") == project {
					m.consume(st.body, "")
				}
			}
		}
		m.consume(stmts, "")
		id := bc.ModuleID(stable("gradle", root+"|"+project))
		s.in.Modules = append(s.in.Modules, bc.Module{ID: id, Name: m.name, Directory: dir})
		// SourceSet conventions are candidates only. A plugin alias or convention
		// plugin can provide Java support even when no literal `java` plugin exists.
		for _, name := range []string{"main", "test"} {
			p := path.Join(dir, "src", name, "java")
			if s.dirs[p] {
				if m.sets[name] == nil {
					m.sets[name] = &gradleSet{name: name, roots: []string{p}}
				}
			}
		}
		var indices []int
		for _, name := range sortedKeys(m.sets) {
			set := m.sets[name]
			if len(set.roots) == 0 {
				set.roots = []string{path.Join(dir, "src", name, "java")}
			}
			release := set.release
			if release == "" {
				release = m.release
			}
			if release == "" {
				release = m.source
			}
			if release == "" {
				release = m.toolchain
			}
			kind := bc.SourceSetCustom
			if name == "main" {
				kind = bc.SourceSetMain
			}
			if name == "test" {
				kind = bc.SourceSetTest
			}
			i := s.addSet(id, name, kind, gradleValue(release, m.props), set.roots)
			s.in.SourceSets[i].EnablePreview = m.preview
			indices = append(indices, i)
		}
		var mainID bc.SourceSetID
		for _, i := range indices {
			if s.in.SourceSets[i].Kind == bc.SourceSetMain {
				mainID = s.in.SourceSets[i].ID
			}
		}
		for _, i := range indices {
			if s.in.SourceSets[i].Kind == bc.SourceSetTest && mainID != "" {
				s.in.SourceSets[i].Classpath = append([]bc.PathEntry{{Kind: bc.EntrySourceSet, RefID: string(mainID)}}, s.in.SourceSets[i].Classpath...)
			}
		}
		s.gap(string(id)+"/gradle-model", "Evaluated Gradle model", "Gradle declarations and conventional source directories are candidates; plugins, conditions, applied scripts, custom tasks and source exclusions were not evaluated", id, "")
		for _, dep := range m.dependencies {
			s.gradleDependency(m, id, dep)
		}
	}
	return nil
}

func (m *gradleModel) set(name string) *gradleSet {
	if m.sets[name] == nil {
		m.sets[name] = &gradleSet{name: name, declared: true}
	}
	return m.sets[name]
}

var javaVersionRE = regexp.MustCompile(`^(?:JavaVersion\.(?:VERSION_|toVersion\())|(?:JavaLanguageVersion\.of\()`)

func javaVersion(ts []token, props map[string]string) string {
	text := compact(ts)
	text = strings.TrimPrefix(text, "=")
	text = strings.TrimPrefix(text, "(")
	text = javaVersionRE.ReplaceAllString(text, "")
	text = strings.TrimRight(text, ")")
	text = strings.ReplaceAll(text, "_", ".")
	if value, ok := props[text]; ok {
		text = value
	}
	return gradleValue(text, props)
}

func (m *gradleModel) consume(stmts []statement, context string) {
	for _, st := range stmts {
		h := compact(st.head)
		if len(st.head) == 0 {
			continue
		}
		if st.body != nil {
			switch {
			case h == "java":
				m.consume(st.body, context)
			case h == "toolchain" || h == "java.toolchain":
				m.consume(st.body, "toolchain")
			case h == "sourceSets":
				m.consume(st.body, "sourceSets")
			case h == "dependencies":
				m.dependencies = append(m.dependencies, st.body...)
			case strings.HasPrefix(h, "tasks.withType<JavaCompile>") || strings.HasPrefix(h, "tasks.withType(JavaCompile)"):
				m.consume(st.body, "compile:all")
			case h == "compileJava" || h == "tasks.compileJava" || h == "tasks.named<JavaCompile>(compileJava)" || h == "tasks.named(compileJava)":
				m.consume(st.body, "compile:main")
			case h == "compileTestJava" || h == "tasks.compileTestJava" || h == "tasks.named<JavaCompile>(compileTestJava)" || h == "tasks.named(compileTestJava)":
				m.consume(st.body, "compile:test")
			case context == "sourceSets":
				name := ""
				if len(st.head) == 1 && !st.head[0].quoted {
					name = first(st)
				} else if len(st.head) >= 4 && first(st) == "val" && st.head[2].text == "by" && (st.head[3].text == "creating" || st.head[3].text == "getting") {
					name = st.head[1].text
				} else if first(st) == "named" || first(st) == "getByName" || first(st) == "create" || first(st) == "register" {
					args, ok := literalArgs(st.head[1:])
					if ok && len(args) == 1 {
						name = args[0]
					}
				}
				if simpleName(name) {
					m.set(name)
					m.consume(st.body, "set:"+name)
				}
			case strings.HasPrefix(context, "set:") && h == "java":
				m.consume(st.body, context)
			}
			continue
		}
		// Compiler properties are recognized only in unconditional known scopes.
		for _, key := range []string{"options.release", "options.release.set", "sourceCompatibility", "targetCompatibility", "languageVersion", "languageVersion.set", "java.sourceCompatibility", "java.targetCompatibility"} {
			prefix := strings.Split(key, ".")
			count := len(prefix)*2 - 1
			if len(st.head) <= count || compact(st.head[:count]) != key {
				continue
			}
			rest := st.head[count:]
			v := javaVersion(rest, m.props)
			switch key {
			case "options.release", "options.release.set":
				if strings.HasPrefix(context, "compile:") {
					name := strings.TrimPrefix(context, "compile:")
					if name == "all" {
						m.release = v
					} else {
						m.set(name).release = v
					}
				}
			case "languageVersion", "languageVersion.set":
				if context == "toolchain" {
					m.toolchain = v
				}
			case "sourceCompatibility", "java.sourceCompatibility":
				if context == "" || context == "compile:all" {
					m.source = v
				} else if strings.HasPrefix(context, "compile:") {
					m.set(strings.TrimPrefix(context, "compile:")).release = v
				}
			}
		}
		if strings.HasPrefix(h, "options.compilerArgs") && strings.HasPrefix(context, "compile:") {
			for _, v := range stringsIn(st.head) {
				if v == "--enable-preview" {
					m.preview = true
				}
			}
		}
		if strings.HasPrefix(context, "set:") {
			m.sourceDirs(m.set(strings.TrimPrefix(context, "set:")), st.head)
		} else if strings.HasPrefix(h, "sourceSets.") {
			if len(st.head) > 4 && st.head[1].text == "." && st.head[3].text == "." {
				name := st.head[2].text
				if simpleName(name) {
					m.sourceDirs(m.set(name), st.head[4:])
				}
			}
		}
	}
}

func simpleName(s string) bool {
	if s == "" || len(s) > 256 {
		return false
	}
	for _, r := range s {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '_' || r == '-') {
			return false
		}
	}
	return true
}

func (m *gradleModel) sourceDirs(set *gradleSet, ts []token) {
	if len(ts) > 2 && ts[0].text == "java" && ts[1].text == "." {
		ts = ts[2:]
	}
	if len(ts) < 2 {
		return
	}
	op := ts[0].text
	if op != "srcDir" && op != "srcDirs" && op != "setSrcDirs" {
		return
	}
	args, ok := sourceArgs(ts[1:])
	if !ok {
		return
	}
	replace := op == "setSrcDirs" || ts[1].text == "="
	if replace {
		set.roots = nil
	} else if len(set.roots) == 0 {
		set.roots = []string{path.Join(m.dir, "src", set.name, "java")}
	}
	for _, raw := range args {
		dir, ok := sourcePath(m.dir, gradleValue(raw, m.props))
		if ok && !contains(set.roots, dir) {
			set.roots = append(set.roots, dir)
		}
	}
}

func (s *scan) gradleDependency(m *gradleModel, module bc.ModuleID, st statement) {
	if len(st.head) < 2 {
		return
	}
	configuration := first(st)
	args, ok := literalArgs(st.head[1:])
	if !ok || len(args) != 1 {
		s.gap(string(module)+"/dependency/"+compact(st.head), "Gradle dependency in "+configuration, "Project, catalog, platform, file or computed dependency requires Gradle evaluation", module, "")
		return
	}
	raw := gradleValue(args[0], m.props)
	parts := strings.Split(raw, ":")
	if len(parts) < 3 || len(parts) > 4 {
		s.gap(string(module)+"/dependency/"+raw, "Gradle dependency in "+configuration, "Dependency coordinate or version was not established", module, "")
		return
	}
	c := bc.Coordinates{Group: parts[0], Name: parts[1], Version: parts[2], Extension: "jar"}
	if len(parts) == 4 {
		c.Classifier = parts[3]
	}
	if i := strings.LastIndex(c.Version, "@"); i >= 0 {
		c.Extension = c.Version[i+1:]
		c.Version = c.Version[:i]
	}
	s.gap(string(module)+"/dependency/"+configuration+raw, "Declared dependency "+raw, "Declared Gradle dependency in "+configuration+" has not been resolved or mapped to effective source-set compiler visibility", module, "")
	if literal(c.Group) && literal(c.Name) && literal(c.Version) && literal(c.Extension) && !strings.ContainsAny(c.Version, "+[](),") && !strings.HasPrefix(c.Version, "latest.") {
		s.artifact(c)
	}
}

// These collection constructors have the same literal directory contents in
// the supported DSL forms. No arbitrary call is evaluated.
func sourceArgs(ts []token) ([]string, bool) {
	var out []token
	for _, t := range ts {
		if !t.quoted && (t.text == "listOf" || t.text == "setOf" || t.text == "files") {
			continue
		}
		out = append(out, t)
	}
	return literalArgs(out)
}
