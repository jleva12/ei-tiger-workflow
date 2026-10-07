package maven

import (
	"strconv"
	"strings"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
)

// Effective models preserve execution-level combine.self. An overriding
// annotation-only execution must not inherit default compilation exclusions.
func mergeConfiguration(parent, child configuration) configuration {
	if child.CombineSelf == "override" {
		return child
	}
	pairs := [][2]*string{{&parent.Release, &child.Release}, {&parent.Source, &child.Source}, {&parent.Target, &child.Target}, {&parent.TestRelease, &child.TestRelease}, {&parent.TestSource, &child.TestSource}, {&parent.TestTarget, &child.TestTarget}, {&parent.Proc, &child.Proc}}
	for _, pair := range pairs {
		if *pair[1] != "" {
			*pair[0] = *pair[1]
		}
	}
	if child.Includes != nil {
		parent.Includes = child.Includes
	}
	if child.Excludes != nil {
		parent.Excludes = child.Excludes
	}
	if child.TestIncludes != nil {
		parent.TestIncludes = child.TestIncludes
	}
	if child.TestExcludes != nil {
		parent.TestExcludes = child.TestExcludes
	}
	if child.CompilerArgs != nil {
		parent.CompilerArgs = child.CompilerArgs
	}
	return parent
}

// compilerLevels returns the effective release, source and target of a main
// or test compilation: the compiler configuration first, then its properties.
func compilerLevels(p project, cfg configuration, test bool) (release, source, target string) {
	release, source, target = cfg.Release, cfg.Source, cfg.Target
	if test {
		if cfg.TestRelease != "" {
			release = cfg.TestRelease
		}
		if cfg.TestSource != "" {
			source = cfg.TestSource
		}
		if cfg.TestTarget != "" {
			target = cfg.TestTarget
		}
		if release == "" {
			release = p.Properties.get("maven.compiler.testRelease")
		}
		if source == "" {
			source = p.Properties.get("maven.compiler.testSource")
		}
		if target == "" {
			target = p.Properties.get("maven.compiler.testTarget")
		}
	}
	if release == "" {
		release = p.Properties.get("maven.compiler.release")
	}
	if source == "" {
		source = p.Properties.get("maven.compiler.source")
	}
	if target == "" {
		target = p.Properties.get("maven.compiler.target")
	}
	return release, source, target
}

func (s *observer) compilerSettings(p project, set *bc.SourceSet, cfg configuration, test bool) bool {
	release, source, target := compilerLevels(p, cfg, test)
	set.IncludePatterns, set.ExcludePatterns = cfg.Includes, cfg.Excludes
	if test {
		// TestCompilerMojo has separate testIncludes/testExcludes parameters;
		// the compile goal's includes/excludes do not constrain test inputs.
		set.IncludePatterns, set.ExcludePatterns = cfg.TestIncludes, cfg.TestExcludes
	}
	// Levels outside what the analysis JDK compiles are brought inside it:
	// Java 6 or 7 sources are Java 8 sources to javac --release 8, and a
	// level newer than the JDK is the newest it has. An absent level is what
	// current compiler plugins default to, 8; an unreadable one (a property
	// Maven left unresolved) the newest, which reads older code.
	level := func(text string, fallback int) int {
		text = strings.TrimSpace(text)
		if text == "" {
			return fallback
		}
		v, err := strconv.Atoi(strings.TrimPrefix(text, "1."))
		if err != nil || v <= 0 {
			s.gap(set.ModuleID, "", "compiler_level:"+string(set.ID), gapText("Its Java level "+text+" is not a number; it is analysed as the newest Java the analysis supports"))
			return s.major
		}
		return min(max(v, 8), s.major)
	}
	version := release
	set.LanguageOptions = map[string]string{}
	if version == "" {
		version = source
		set.LanguageOptions["java.compiler.mode"] = "source-target"
		set.LanguageOptions["java.compiler.target"] = strconv.Itoa(level(target, level(source, 8)))
	}
	set.TargetRelease = level(version, 8)
	set.LanguageVersion = strconv.Itoa(set.TargetRelease)
	if cfg.Proc != "" {
		set.LanguageOptions["java.compiler.proc"] = cfg.Proc
	}
	return true
}

func (s *observer) compilerExecutions(p project, module bc.ModuleID) error {
	var main *bc.SourceSet
	for i := range s.inventory.SourceSets {
		set := &s.inventory.SourceSets[i]
		if set.ModuleID == module && set.Kind == bc.SourceSetMain {
			main = set
			break
		}
	}
	if main == nil {
		return nil
	}
	// Copy before appending; SourceSets may grow while creating execution variants.
	base := *main
	for _, plugin := range p.Build.Plugins {
		if plugin.Name != "maven-compiler-plugin" {
			continue
		}
		for _, execution := range plugin.Executions {
			if execution.ID == "default-compile" || execution.ID == "default-testCompile" {
				continue
			}
			for _, goal := range execution.Goals {
				if goal != "compile" && goal != "testCompile" {
					continue
				}
				cfg := mergeConfiguration(plugin.Configuration, execution.Configuration)
				if goal != "compile" || cfg.Proc != "only" || execution.ID == "" || execution.Phase == "" {
					s.gap(module, "", "compiler_execution:"+execution.ID, "Additional compiler execution is not represented by an observed compilation context")
					continue
				}
				set := base
				set.ID = bc.SourceSetID(string(module) + ":execution:" + execution.ID)
				set.Name, set.Kind = execution.ID, bc.SourceSetCustom
				set.OutputInputID = "" // proc:only has no class output for dependent sets.
				set.Classpath, set.ModulePath = nil, nil
				if !s.compilerSettings(p, &set, cfg, false) {
					continue
				}
				set.LanguageOptions["java.maven.execution"] = execution.ID
				set.LanguageOptions["java.maven.phase"] = execution.Phase
				s.inventory.SourceSets = append(s.inventory.SourceSets, set)
			}
		}
	}
	return nil
}
