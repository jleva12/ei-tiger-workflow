package maven

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/worker/internal/buildcontext/manifest"
	"ei-aitiger-codegraph/worker/internal/scratch"
)

// Config enables explicit local build preparation. Maven's install lifecycle
// executes build plugins and source generators. Tests never execute. Consumed
// test-helper producers and their reactor prerequisites receive scoped preparation.
// No deploy/upload goal is invoked. All writes belong to the
// service-owned checkout/cache/scratch directories supplied by the caller.
type Config struct {
	JavaHome string
	// BuildJavaHomes maps a JDK's major version to its home. Maven runs with
	// the oldest of them that can compile the project, or JavaHome when none
	// is older; the source sets are still attributed with JavaHome.
	BuildJavaHomes  map[int]string
	MavenExecutable string
	CacheDir        string
	WorkDir         string
	Timeout         time.Duration
	MaxModelBytes   int64
	MaxLogBytes     int64
}
type Provider struct {
	config Config
	// Build JDKs older than the service JDK, oldest first.
	buildJDKs []buildJDK
}

type buildJDK struct {
	major int
	home  string
}

func New(c Config) (*Provider, error) {
	for _, name := range []string{c.JavaHome, c.MavenExecutable, c.CacheDir, c.WorkDir} {
		if !filepath.IsAbs(name) || strings.ContainsRune(name, 0) {
			return nil, bc.ErrInvalidInput
		}
	}
	for major, home := range c.BuildJavaHomes {
		if major < 8 || !filepath.IsAbs(home) || strings.ContainsRune(home, 0) {
			return nil, bc.ErrInvalidInput
		}
	}
	if c.Timeout == 0 {
		c.Timeout = 45 * time.Minute
	}
	if c.MaxModelBytes == 0 {
		c.MaxModelBytes = 128 << 20
	}
	if c.MaxLogBytes == 0 {
		c.MaxLogBytes = 64 << 20
	}
	if c.Timeout <= 0 || c.MaxModelBytes <= 0 || c.MaxModelBytes > 512<<20 || c.MaxLogBytes <= 0 || c.MaxLogBytes > 256<<20 {
		return nil, bc.ErrInvalidInput
	}
	executables := []string{c.MavenExecutable, filepath.Join(c.JavaHome, "bin", "java")}
	for _, home := range c.BuildJavaHomes {
		executables = append(executables, filepath.Join(home, "bin", "java"))
	}
	for _, name := range executables {
		info, err := os.Stat(name)
		if err != nil {
			return nil, err
		}
		if !info.Mode().IsRegular() || info.Mode()&0111 == 0 {
			return nil, bc.ErrInvalidInput
		}
	}
	for _, dir := range []string{c.CacheDir, c.WorkDir} {
		if err := os.MkdirAll(dir, 0700); err != nil {
			return nil, err
		}
	}
	scratch.Sweep(c.WorkDir, "maven-inputs-", scratch.Stale) // builds a killed process left
	p := &Provider{config: c}
	if len(c.BuildJavaHomes) > 0 {
		service, err := readJDK(c.JavaHome)
		if err != nil {
			return nil, err
		}
		for major, home := range c.BuildJavaHomes {
			actual, err := jdkMajor(home)
			if err != nil {
				return nil, fmt.Errorf("build JDK %d: %w", major, err)
			}
			if actual != major {
				return nil, fmt.Errorf("%w: build JDK %d at %s is Java %d", bc.ErrInvalidInput, major, home, actual)
			}
			if major < service.major {
				p.buildJDKs = append(p.buildJDKs, buildJDK{major: major, home: home})
			}
		}
		sort.Slice(p.buildJDKs, func(i, j int) bool { return p.buildJDKs[i].major < p.buildJDKs[j].major })
	}
	return p, nil
}
func (p *Provider) Build(ctx context.Context, r bc.Request) (bc.BuildContext, error) {
	if err := r.Validate(); err != nil {
		return bc.BuildContext{}, err
	}
	// A checkout without any POM has no Maven build to prepare; a caller that
	// composes languages covers Java with its syntax profile instead.
	if found, err := hasPOM(ctx, r); err != nil {
		return bc.BuildContext{}, err
	} else if !found {
		return bc.BuildContext{}, fmt.Errorf("%w: no pom.xml in the checkout", bc.ErrNoBuild)
	}
	ctx, cancel := context.WithTimeout(ctx, p.config.Timeout)
	defer cancel()
	started := time.Now()
	cache, err := p.fingerprints()
	if err != nil {
		return bc.BuildContext{}, err
	}
	fingerprint, err := p.buildFingerprint(ctx, r)
	if err != nil {
		return bc.BuildContext{}, err
	}
	build, reason, err := p.loadCached(ctx, r, fingerprint, cache)
	if err != nil {
		return bc.BuildContext{}, err
	}
	logCacheDecision(ctx, r, fingerprint, reason)
	if reason == "" {
		p.saveFingerprints(ctx, cache)
		slog.InfoContext(ctx, "Maven compilation inputs prepared", "repository_id", r.Checkout.RepositoryID, "cache_hit", true, "maven_invocations", 0, "duration", time.Since(started))
		return build, nil
	}
	scratch, err := os.MkdirTemp(p.config.WorkDir, "maven-inputs-*")
	if err != nil {
		return bc.BuildContext{}, err
	}
	defer os.RemoveAll(scratch)
	repository := filepath.Join(p.config.CacheDir, "repository")
	if err = os.MkdirAll(repository, 0700); err != nil {
		return bc.BuildContext{}, err
	}
	// -T 1C builds independent reactor modules concurrently. Every stage first
	// runs offline against the isolated repository and retries online only
	// when Maven reports an artifact it has not downloaded before.
	common := append([]string{"-B", "-ntp", "-T", "1C", "-Dmaven.repo.local=" + repository, "-Dmaven.test.skip=true", "-DskipTests"}, skippedPlugins...)
	pom, err := rootPOM(ctx, r, scratch)
	if err != nil {
		return bc.BuildContext{}, err
	}
	if pom != "" {
		common = append(common, "-f", pom)
	}
	runner := &mavenRunner{provider: p, ctx: ctx, request: r, scratch: scratch, javaHome: p.config.JavaHome, common: common}
	model := filepath.Join(scratch, "effective-pom.xml")
	// The effective model depends on POM files, settings and active profiles,
	// not on build outputs, so one observation serves every later decision.
	// A cold reactor may consume an attached tests JAR before its producer is
	// installed; observe Maven's effective dependencies before the main install.
	if err = runner.run("reactor-model", effectivePOM(model)...); err != nil {
		return bc.BuildContext{}, err
	}
	invocations := 0
	jdk, found, err := p.buildJDK(ctx, r, model)
	if err != nil {
		return bc.BuildContext{}, err
	}
	var matchedErr error
	// The sources javac rejected in the build that went through, and the
	// steps that failed in it without stopping it.
	var failures []compileError
	var stageFailures []stageFailure
	if found {
		// A new JDK can reject what an old build depends on, such as JARs its
		// zip checks refuse or profiles activated by JDK version; the project's
		// own JDK builds it as it was written. Profiles may differ, so the
		// model is observed again under it.
		slog.InfoContext(ctx, "Maven runs with the JDK the project targets", "repository_id", r.Checkout.RepositoryID, "jdk", jdk.major)
		// Strict: a failure that is not javac's is the service JDK's to try
		// before anything is built past it.
		matched := &mavenRunner{provider: p, ctx: ctx, request: r, scratch: scratch, javaHome: jdk.home, label: fmt.Sprintf("jdk%d-", jdk.major), common: common, strict: true}
		matchedModel := filepath.Join(scratch, fmt.Sprintf("effective-pom-jdk%d.xml", jdk.major))
		matchedErr = matched.run("reactor-model", effectivePOM(matchedModel)...)
		if matchedErr == nil {
			matchedErr = p.prepare(ctx, r, matched, matchedModel)
		}
		invocations += matched.invocations
		if matchedErr == nil {
			model, failures, stageFailures = matchedModel, matched.compileErrors, matched.stageFailures
		} else if ctx.Err() != nil {
			return bc.BuildContext{}, matchedErr
		} else {
			slog.WarnContext(ctx, "Maven failed with the JDK the project targets; retrying with the service JDK", "repository_id", r.Checkout.RepositoryID, "jdk", jdk.major, "error", matchedErr)
		}
	}
	if !found || matchedErr != nil {
		err = p.prepare(ctx, r, runner, model)
		invocations += runner.invocations
		if err != nil {
			if matchedErr != nil {
				err = fmt.Errorf("with JDK %d: %v; with the service JDK: %w", jdk.major, matchedErr, err)
			}
			return bc.BuildContext{}, err
		}
		failures, stageFailures = runner.compileErrors, runner.stageFailures
	}
	build, err = p.observe(ctx, r, model, cache, failures, stageFailures)
	if err != nil {
		return bc.BuildContext{}, err
	}
	if len(failures) > 0 || len(stageFailures) > 0 {
		// Another run of the same snapshot builds again rather than reusing
		// output that is missing, e.g. after a build JDK is added.
		slog.WarnContext(ctx, "Maven could not build everything; what it left out is recorded as gaps", "repository_id", r.Checkout.RepositoryID, "files", len(failures), "failed_steps", len(stageFailures))
	} else if err = p.storeCached(ctx, r, fingerprint, build); err != nil {
		if ctx.Err() != nil {
			return bc.BuildContext{}, ctx.Err()
		}
		slog.WarnContext(ctx, "Maven build context was not cached", "repository_id", r.Checkout.RepositoryID, "error", err)
	}
	p.saveFingerprints(ctx, cache)
	slog.InfoContext(ctx, "Maven compilation inputs prepared", "repository_id", r.Checkout.RepositoryID, "cache_hit", false, "maven_invocations", invocations, "duration", time.Since(started))
	return build, nil
}

func effectivePOM(output string) []string {
	return []string{"org.apache.maven.plugins:maven-help-plugin:3.5.1:effective-pom", "-Doutput=" + output}
}

// prepare runs the stages after the reactor model with one runner's JDK.
func (p *Provider) prepare(ctx context.Context, r bc.Request, runner *mavenRunner, model string) error {
	helperArtifacts, err := p.declaredTestProjects(ctx, r, model)
	if err != nil {
		return err
	}
	if len(helperArtifacts) > 0 {
		if err = runner.compile("test-helper-artifacts", "-Dmaven.test.skip=false", "-DskipTests", "-pl", strings.Join(helperArtifacts, ","), "-am", "install"); err != nil {
			return err
		}
	}
	// install is required, not optional: every main source set pins its
	// target/classes as the compiled output that dependent source sets see on
	// the classpath, and dependency:build-classpath lists reactor siblings as
	// repository JARs only once they are installed.
	if err = runner.compile("prepare", "install"); err != nil {
		return err
	}
	if err = runner.tolerant("compile-classpath", "org.apache.maven.plugins:maven-dependency-plugin:3.8.1:build-classpath", "-DincludeScope=compile", "-Dmdep.outputFile=target/codegraph-compile-classpath.txt"); err != nil {
		return err
	}
	if err = runner.tolerant("test-classpath", "org.apache.maven.plugins:maven-dependency-plugin:3.8.1:build-classpath", "-DincludeScope=test", "-Dmdep.outputFile=target/codegraph-test-classpath.txt"); err != nil {
		return err
	}
	helperProjects, err := p.consumedTestProjects(ctx, r, model)
	if err != nil {
		return err
	}
	if len(helperProjects) > 0 {
		// The test-compile phase does not reach test execution. Only reactor
		// test outputs consumed as dependency artifacts require this step. It
		// changes neither the effective model nor the resolved classpaths;
		// the observer reads test-classes directories directly.
		if err = runner.compile("test-helper-inputs", "-Dmaven.test.skip=false", "-DskipTests", "-pl", strings.Join(helperProjects, ","), "test-compile"); err != nil {
			return err
		}
	}
	return p.materializeDeclaredRoots(ctx, r, model)
}

// buildJDK picks the oldest build JDK that can compile every module of the
// effective model. A module that sets release needs javac's --release, so
// JDK 9 or later; source and target alone compile on that version's JDK.
// found is false when the service JDK is the choice, including when some
// module's level can't be read.
func (p *Provider) buildJDK(ctx context.Context, r bc.Request, model string) (jdk buildJDK, found bool, err error) {
	if len(p.buildJDKs) == 0 {
		return buildJDK{}, false, nil
	}
	projects, err := readProjects(ctx, model, r.Limits, p.config.MaxModelBytes)
	if err != nil {
		return buildJDK{}, false, err
	}
	need := 0
	for _, project := range projects {
		if project.Packaging == "pom" {
			continue // aggregators compile nothing
		}
		level, ok := requiredJDK(project)
		if !ok {
			return buildJDK{}, false, nil
		}
		need = max(need, level)
	}
	if need == 0 {
		return buildJDK{}, false, nil
	}
	for _, jdk := range p.buildJDKs {
		if jdk.major >= need {
			return jdk, true, nil
		}
	}
	return buildJDK{}, false, nil
}

// requiredJDK is the lowest JDK major version that compiles a project's main
// and test sources at their effective settings.
func requiredJDK(p project) (int, bool) {
	var main, test configuration
	for _, plug := range p.Build.Plugins {
		if plug.Name != "maven-compiler-plugin" {
			continue
		}
		main, test = plug.Configuration, plug.Configuration
		for _, execution := range plug.Executions {
			switch execution.ID {
			case "default-compile":
				main = mergeConfiguration(main, execution.Configuration)
			case "default-testCompile":
				test = mergeConfiguration(test, execution.Configuration)
			}
		}
	}
	need := 0
	for _, set := range []struct {
		cfg  configuration
		test bool
	}{{main, false}, {test, true}} {
		release, source, target := compilerLevels(p, set.cfg, set.test)
		if release != "" {
			level, ok := javaLevel(release)
			if !ok {
				return 0, false
			}
			need = max(need, level, 9)
			continue
		}
		if source == "" && target == "" {
			return 0, false
		}
		for _, text := range []string{source, target} {
			if text == "" {
				continue
			}
			level, ok := javaLevel(text)
			if !ok {
				return 0, false
			}
			need = max(need, level)
		}
	}
	return need, true
}

func javaLevel(text string) (int, bool) {
	level, err := strconv.Atoi(strings.TrimPrefix(text, "1."))
	return level, err == nil && level >= 1
}

func (p *Provider) saveFingerprints(ctx context.Context, cache *manifest.FingerprintCache) {
	if err := cache.Save(); err != nil {
		slog.WarnContext(ctx, "fingerprint cache was not saved", "error", err)
	}
}

type mavenRunner struct {
	provider *Provider
	ctx      context.Context
	request  bc.Request
	scratch  string
	javaHome string
	// label prefixes the stage logs so runners with different JDKs can share
	// the scratch directory.
	label       string
	common      []string
	online      bool
	invocations int
	// lastLog is the log of the latest attempt.
	lastLog string
	// pastErrors is set once a compiling stage failed: later compiling
	// stages go straight past the same errors.
	pastErrors    bool
	compileErrors []compileError
	// serial is set once a stage failed other than in javac, or ran out of
	// memory: later stages build one module at a time with a larger heap,
	// which avoids the races and memory use of a parallel reactor.
	serial bool
	// strict runners go past javac's errors only; any other failure is
	// returned, for another runner to try.
	strict bool
	// stageFailures are the steps that failed in a stage the build went
	// past, per project, for the gaps of what they left out.
	stageFailures []stageFailure
}

// stageFailure is one "Failed to execute goal … on project <name>" line of
// a stage the build went past.
type stageFailure struct {
	project string // the artifactId Maven names; empty when it names none
	line    string
}

// skippedPlugins are the plugins a build for code analysis never needs and
// that fail most often where it runs: without a network, keys, a Docker
// daemon, a Git directory or Node. None of them writes a class file or a
// source file javac reads. Source generators (protoc, OpenAPI, jOOQ, exec
// and antrun steps) are not skipped: javac may need what they write.
var skippedPlugins = []string{
	"-Dcheckstyle.skip=true", "-Dspotless.skip=true", "-Dspotless.check.skip=true", "-Drat.skip=true", "-Djacoco.skip=true",
	"-Dmaven.javadoc.skip=true", "-Dmaven.source.skip=true", "-Dmaven.site.skip=true",
	"-Denforcer.skip=true", "-Dgpg.skip=true", "-Dinvoker.skip=true", "-DskipITs=true",
	"-Dspotbugs.skip=true", "-Dfindbugs.skip=true", "-Dpmd.skip=true", "-Dcpd.skip=true", "-Danimal.sniffer.skip=true",
	"-Dlicense.skip=true", "-Dlicense.skipCheckLicense=true", "-Dlicense.skipDownloadLicenses=true", "-Dlicense.skipAddThirdParty=true",
	"-Dmaven.gitcommitid.skip=true", "-Dmaven.buildNumber.skip=true",
	"-Ddocker.skip=true", "-Ddockerfile.skip=true", "-Djib.skip=true",
	"-Dspring-boot.repackage.skip=true", "-Dspring-boot.build-image.skip=true", "-Dquarkus.build.skip=true", "-DskipNativeBuild=true",
	"-Dcyclonedx.skip=true", "-Ddependency-check.skip=true", "-Dossindex.skip=true", "-Dsonar.skip=true",
	"-Dmdep.analyze.skip=true", "-Dduplicate-finder.skip=true", "-Djapicmp.skip=true", "-Drevapi.skip=true", "-Dclirr.skip=true",
	"-Dsort.skip=true", "-Dfmt.skip=true", "-Dformatter.skip=true", "-Dimpsort.skip=true", "-Dtidy.skip=true",
	"-Dproguard.skip=true", "-Dassembly.skipAssembly=true", "-Dlombok.delombok.skip=true", "-Dgatling.skip=true",
	// frontend-maven-plugin: the web client is not Java.
	"-Dskip.installnodenpm=true", "-Dskip.installnodepnpm=true", "-Dskip.installnodecorepack=true", "-Dskip.installyarn=true", "-Dskip.installbun=true",
	"-Dskip.npm=true", "-Dskip.npx=true", "-Dskip.pnpm=true", "-Dskip.yarn=true", "-Dskip.corepack=true", "-Dskip.bun=true",
	"-Dskip.bower=true", "-Dskip.grunt=true", "-Dskip.gulp=true", "-Dskip.jspm=true", "-Dskip.karma=true", "-Dskip.webpack=true", "-Dskip.ember=true",
}

// compileError is one error javac reported for a checkout source file.
type compileError struct {
	// file is checkout-relative and slash-separated.
	file string
	// line is Maven's [ERROR] line with the path made relative.
	line string
}

// failOnErrorOff keeps Maven going when javac rejects sources: the module's
// classes are missing, but it is still packaged and installed, so the rest of
// the reactor and the classpath stages run.
const failOnErrorOff = "-Dmaven.compiler.failOnError=false"

// pastErrorFlags are the flags of a stage run past its failures: javac's
// errors leave a module without classes but still packaged, and any other
// failed module leaves the rest of the reactor building (the modules that
// depend on it are skipped).
var pastErrorFlags = []string{failOnErrorOff, "--fail-at-end"}

// compile runs a stage that compiles sources. When it fails, it runs again
// past the failures, one module at a time when the failure was not javac's
// (a plugin that broke, or a parallel build that raced): javac's errors are
// kept, and so is every step that still failed. The observer leaves out the
// output those failures left missing and records why; ingestion analyses
// those sets from source, compiling what it can itself.
func (m *mavenRunner) compile(stage string, args ...string) error {
	if !m.pastErrors {
		err := m.run(stage, args...)
		if err == nil || m.ctx.Err() != nil {
			return err
		}
		compileErrors := logHasLine(m.lastLog, compilationFailure)
		if !compileErrors {
			if m.strict {
				return err
			}
			m.serial = true
		}
		slog.WarnContext(m.ctx, "a Maven stage failed; building past the failures", "stage", stage, "javac_errors", compileErrors, "serial", m.serial, "repository_id", m.request.Checkout.RepositoryID, "error", err)
		m.pastErrors = true
		stage += "-past-errors"
	}
	err := m.run(stage, append(append([]string{}, pastErrorFlags...), args...)...)
	if m.ctx.Err() != nil {
		return err
	}
	if collectErr := m.collectCompileErrors(m.lastLog); collectErr != nil {
		return errors.Join(err, collectErr)
	}
	if err != nil {
		if m.strict && !logHasLine(m.lastLog, m.isCompileError) {
			return err
		}
		m.recordFailures(m.lastLog)
		slog.WarnContext(m.ctx, "Maven steps failed past which the build went on; what they left out becomes gaps", "stage", stage, "repository_id", m.request.Checkout.RepositoryID, "error", err)
	}
	return nil
}

// tolerant runs a stage that reads the build without compiling it, past the
// failures of single modules: a module whose dependencies cannot be resolved
// has no classpath, which the observer records as its gap.
func (m *mavenRunner) tolerant(stage string, args ...string) error {
	err := m.run(stage, append([]string{"--fail-at-end"}, args...)...)
	if err == nil || m.ctx.Err() != nil || m.strict {
		return err
	}
	m.recordFailures(m.lastLog)
	slog.WarnContext(m.ctx, "a Maven stage failed for some modules; they are recorded as gaps", "stage", stage, "repository_id", m.request.Checkout.RepositoryID, "error", err)
	return nil
}

// maxStageFailures bounds the failed steps kept from one build.
const maxStageFailures = 200

// recordFailures keeps the "Failed to execute goal" lines of a stage log,
// with the project each names, paths made relative.
func (m *mavenRunner) recordFailures(logPath string) {
	f, err := os.Open(logPath)
	if err != nil {
		return
	}
	defer f.Close()
	paths := strings.NewReplacer(m.request.Checkout.Path+string(filepath.Separator), "", filepath.Join(m.provider.config.CacheDir, "repository")+string(filepath.Separator), "<maven-repository>/")
	scan := bufio.NewScanner(f)
	scan.Buffer(make([]byte, 64<<10), 1<<20)
	for scan.Scan() && len(m.stageFailures) < maxStageFailures {
		text, ok := strings.CutPrefix(strings.TrimRight(scan.Text(), "\r"), "[ERROR] ")
		if !ok || !strings.HasPrefix(text, "Failed to execute goal") {
			continue
		}
		if i := strings.Index(text, " -> [Help"); i >= 0 {
			text = text[:i]
		}
		project := ""
		if _, rest, ok := strings.Cut(text, " on project "); ok {
			project, _, _ = strings.Cut(rest, ":")
			project = strings.TrimSpace(project)
		}
		m.stageFailures = append(m.stageFailures, stageFailure{project: project, line: paths.Replace(text)})
	}
}

// isCompileError matches an [ERROR] line javac wrote for a checkout source.
func (m *mavenRunner) isCompileError(line string) bool {
	text, ok := strings.CutPrefix(line, "[ERROR] ")
	if !ok {
		return false
	}
	name, rest, ok := strings.Cut(text, ".java:[")
	return ok && filepath.IsAbs(name) && strings.Contains(rest, "] ")
}

// compilationFailure matches the line Maven ends a build with when javac
// rejected the sources, not when the compiler could not run at all.
func compilationFailure(line string) bool {
	return strings.HasPrefix(line, "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:") && strings.Contains(line, "Compilation failure")
}

// maxCompileErrors bounds the errors kept from one build.
const maxCompileErrors = 10000

// collectCompileErrors keeps the [ERROR] lines of a stage log that javac
// wrote for a source file in the checkout: "<path>.java:[line,col] message".
func (m *mavenRunner) collectCompileErrors(logPath string) error {
	f, err := os.Open(logPath)
	if err != nil {
		return err
	}
	defer f.Close()
	roots := []string{m.request.Checkout.Path}
	if real, err := filepath.EvalSymlinks(m.request.Checkout.Path); err == nil && real != roots[0] {
		roots = append(roots, real)
	}
	seen := map[compileError]bool{}
	for _, e := range m.compileErrors {
		seen[e] = true
	}
	scan := bufio.NewScanner(f)
	scan.Buffer(make([]byte, 64<<10), 1<<20)
	for scan.Scan() && len(m.compileErrors) < maxCompileErrors {
		text, ok := strings.CutPrefix(strings.TrimRight(scan.Text(), "\r"), "[ERROR] ")
		if !ok {
			continue
		}
		name, rest, ok := strings.Cut(text, ".java:[")
		if !ok || !filepath.IsAbs(name) || !strings.Contains(rest, "] ") {
			continue
		}
		for _, root := range roots {
			relative, err := filepath.Rel(root, filepath.Clean(name+".java"))
			if err != nil {
				continue
			}
			relative = filepath.ToSlash(relative)
			if !bc.ValidPath(relative) {
				continue
			}
			e := compileError{file: relative, line: relative + ":[" + strings.TrimSpace(rest)}
			if !seen[e] {
				seen[e] = true
				m.compileErrors = append(m.compileErrors, e)
			}
			break
		}
	}
	return scan.Err()
}

// run executes one Maven stage. Offline resolution is attempted first; when
// Maven reports that an artifact was never downloaded, the stage is retried
// online and later stages stay online for this build. Any other failure is
// final: a compilation error is never worth a second identical run.
func (m *mavenRunner) run(stage string, args ...string) error {
	err := m.runOnce(stage, args...)
	if err != nil && m.ctx.Err() == nil && !m.serial && logContains(m.lastLog, "java.lang.OutOfMemoryError") {
		// A parallel reactor holds every module's model at once.
		m.serial = true
		slog.WarnContext(m.ctx, "Maven ran out of memory; building one module at a time with a larger heap", "stage", stage, "repository_id", m.request.Checkout.RepositoryID)
		err = m.runOnce(stage+"-serial", args...)
	}
	return err
}

func (m *mavenRunner) runOnce(stage string, args ...string) error {
	base := append([]string{}, m.common...)
	for _, arg := range args {
		if arg == "-Dmaven.test.skip=false" {
			for i, flag := range base {
				if flag == "-Dmaven.test.skip=true" {
					base = append(base[:i], base[i+1:]...)
					break
				}
			}
			break
		}
	}
	if m.serial {
		for i := 0; i+1 < len(base); i++ {
			if base[i] == "-T" {
				base[i+1] = "1"
			}
		}
	}
	if !m.online {
		unresolved, err := m.attempt(stage, true, base, args)
		if err == nil || !unresolved {
			return err
		}
		m.online = true
	}
	_, err := m.attempt(stage, false, base, args)
	return err
}

func (m *mavenRunner) attempt(stage string, offline bool, base, args []string) (bool, error) {
	m.invocations++
	started := time.Now()
	flags := append([]string{}, base...)
	logName := m.label + stage + ".log"
	if offline {
		flags = append([]string{"-o"}, flags...)
	} else {
		logName = m.label + stage + "-online.log"
	}
	slog.InfoContext(m.ctx, "preparing Maven compilation inputs", "stage", stage, "offline", offline, "repository_id", m.request.Checkout.RepositoryID)
	command := exec.CommandContext(m.ctx, m.provider.config.MavenExecutable, append(flags, args...)...)
	command.Dir = m.request.Checkout.Path
	heap := "-Xmx1536m"
	if m.serial {
		heap = "-Xmx3g"
	}
	command.Env = append(os.Environ(), "JAVA_HOME="+m.javaHome, "MAVEN_OPTS="+heap+" -Djava.awt.headless=true")
	logPath := filepath.Join(m.scratch, logName)
	m.lastLog = logPath
	file, err := os.OpenFile(logPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return false, err
	}
	keep := min(m.provider.config.MaxLogBytes/2, 8<<20)
	output := &boundedLog{writer: file, remaining: m.provider.config.MaxLogBytes - keep, keep: int(keep)}
	command.Stdout = output
	command.Stderr = output
	err = runOwnedCommand(command)
	closeErr := errors.Join(output.finish(), file.Close())
	duration := time.Since(started)
	if m.ctx.Err() != nil {
		return false, m.ctx.Err()
	}
	if err != nil {
		unresolved := offline && logContains(logPath, "offline mode")
		slog.WarnContext(m.ctx, "Maven stage failed", "stage", stage, "offline", offline, "duration", duration, "retry_online", unresolved, "repository_id", m.request.Checkout.RepositoryID)
		return unresolved, fmt.Errorf("Maven %s failed: %w; %s", stage, err, m.errors(logPath, output.tail))
	}
	slog.InfoContext(m.ctx, "Maven stage completed", "stage", stage, "offline", offline, "duration", duration, "repository_id", m.request.Checkout.RepositoryID)
	return false, closeErr
}

// mavenFooter starts the help Maven prints after every failure.
var mavenFooter = []string{"-> [Help", "[Help", "To see the full stack trace", "Re-run Maven using", "For more information about the errors", "After correcting the problems", "mvn <args> -rf"}

// errors returns the [ERROR] lines of a failed stage's log, up to 4 KB,
// without Maven's help footer, with checkout paths made relative and the
// Maven repository written as <maven-repository>. Maven's "Failed to execute
// goal" lines come first: they name the step that failed, and past compile
// errors that is not the compiler. The other lines follow in log order, the
// cause first: javac reports an unreadable JAR before the hundreds of errors
// it causes, which fill the tail. Without such lines it returns the tail.
func (m *mavenRunner) errors(logPath string, tail []byte) string {
	paths := strings.NewReplacer(m.request.Checkout.Path+string(filepath.Separator), "", filepath.Join(m.provider.config.CacheDir, "repository")+string(filepath.Separator), "<maven-repository>/")
	var goals, lines []string
	size := 0
	if f, err := os.Open(logPath); err == nil {
		defer f.Close()
		scan := bufio.NewScanner(f)
		scan.Buffer(make([]byte, 64<<10), 1<<20)
	next:
		for scan.Scan() {
			text, ok := strings.CutPrefix(strings.TrimRight(scan.Text(), "\r"), "[ERROR]")
			text = strings.TrimSpace(text)
			if !ok || text == "" {
				continue
			}
			for _, footer := range mavenFooter {
				if strings.HasPrefix(text, footer) {
					continue next
				}
			}
			if strings.HasPrefix(text, "Failed to execute goal") {
				if i := strings.Index(text, " -> [Help"); i >= 0 {
					text = text[:i]
				}
				if len(goals) < 8 {
					goals = append(goals, "[ERROR] "+paths.Replace(text))
				}
				continue
			}
			if size < 4096 {
				line := "[ERROR] " + paths.Replace(text)
				lines = append(lines, line)
				size += len(line) + 1
			}
		}
	}
	lines = append(goals, lines...)
	if len(lines) == 0 {
		return strings.TrimSpace(string(tail))
	}
	return strings.Join(lines, "\n")
}

// logHasLine reports whether a line of a bounded Maven log matches.
func logHasLine(name string, match func(string) bool) bool {
	f, err := os.Open(name)
	if err != nil {
		return false
	}
	defer f.Close()
	scan := bufio.NewScanner(f)
	scan.Buffer(make([]byte, 64<<10), 1<<20)
	for scan.Scan() {
		if match(strings.TrimRight(scan.Text(), "\r")) {
			return true
		}
	}
	return false
}

// logContains scans a bounded Maven log for needle without loading it whole.
func logContains(name, needle string) bool {
	f, err := os.Open(name)
	if err != nil {
		return false
	}
	defer f.Close()
	buffer := make([]byte, 0, 64<<10)
	chunk := make([]byte, 64<<10)
	for {
		n, err := f.Read(chunk)
		buffer = append(buffer, chunk[:n]...)
		if strings.Contains(string(buffer), needle) {
			return true
		}
		if len(buffer) > len(needle) {
			buffer = append(buffer[:0], buffer[len(buffer)-len(needle):]...)
		}
		if err != nil {
			return false
		}
	}
}

// boundedLog keeps a Maven log within its budget without ever failing the
// build for talking too much: the head is written as it comes, then only the
// last keep bytes are held, and finish writes them after a marker. Maven
// ends a failed build with its errors, so they survive. tail is the last
// 8 KB, for a failure message.
type boundedLog struct {
	writer    io.Writer
	remaining int64
	keep      int
	held      []byte
	dropped   int64
	tail      []byte
}

func (w *boundedLog) Write(b []byte) (int, error) {
	total := len(b)
	w.tail = append(w.tail, b[max(0, len(b)-8192):]...)
	if len(w.tail) > 8192 {
		w.tail = w.tail[len(w.tail)-8192:]
	}
	if w.remaining > 0 {
		n := int(min(int64(len(b)), w.remaining))
		if _, err := w.writer.Write(b[:n]); err != nil {
			return 0, err
		}
		w.remaining -= int64(n)
		b = b[n:]
	}
	if len(b) > 0 {
		w.held = append(w.held, b...)
		// Trimmed once it doubles, so each byte is copied a bounded
		// number of times however the log is written.
		if len(w.held) > 2*w.keep {
			w.trim()
		}
	}
	return total, nil
}

// trim keeps the last keep bytes held, dropping whole lines where possible
// so every kept line is intact.
func (w *boundedLog) trim() {
	over := len(w.held) - w.keep
	if over <= 0 {
		return
	}
	cut := over
	if i := bytes.IndexByte(w.held[over:], '\n'); i >= 0 && i < 4096 {
		cut = over + i + 1
	}
	w.dropped += int64(cut)
	w.held = append(w.held[:0], w.held[cut:]...)
}

// finish writes the held end of an overlong log.
func (w *boundedLog) finish() error {
	if len(w.held) == 0 {
		return nil
	}
	w.trim()
	if w.dropped > 0 {
		if _, err := fmt.Fprintf(w.writer, "\n[codegraph: %d bytes of this log were left out]\n", w.dropped); err != nil {
			return err
		}
	}
	_, err := w.writer.Write(w.held)
	w.held = nil
	return err
}

// BuildObserved reads output from the exact checkout's already successful Maven
// preparation. This is useful to resume observation; all paths and bytes are
// validated again. Missing classpaths/generated outputs remain explicit gaps.
// It shares the fingerprint cache but never reads or writes context entries.
func (p *Provider) BuildObserved(ctx context.Context, r bc.Request, model string) (bc.BuildContext, error) {
	if err := r.Validate(); err != nil {
		return bc.BuildContext{}, err
	}
	cache, err := p.fingerprints()
	if err != nil {
		return bc.BuildContext{}, err
	}
	build, err := p.observe(ctx, r, model, cache, nil, nil)
	if err != nil {
		return bc.BuildContext{}, err
	}
	p.saveFingerprints(ctx, cache)
	return build, nil
}

// observe reads the prepared checkout. failures are the sources javac
// rejected; the source sets they belong to get no compiled output.
func (p *Provider) observe(ctx context.Context, r bc.Request, model string, cache *manifest.FingerprintCache, failures []compileError, stageFailures []stageFailure) (bc.BuildContext, error) {
	projects, err := readProjects(ctx, model, r.Limits, p.config.MaxModelBytes)
	if err != nil {
		return bc.BuildContext{}, err
	}
	checkout, err := filepath.EvalSymlinks(r.Checkout.Path)
	if err != nil {
		return bc.BuildContext{}, err
	}
	r.Checkout.Path = checkout
	s := observer{ctx: ctx, request: r, config: p.config, cache: cache, failures: failures, stageFailures: stageFailures, inputs: map[bc.InputID]bool{}, artifacts: map[bc.ArtifactID]bool{}, projectDirs: map[string]string{}, sets: map[string]bc.SourceSetID{}}
	if err = s.locateProjects(); err != nil {
		return bc.BuildContext{}, err
	}
	if err = s.jdk(); err != nil {
		return bc.BuildContext{}, err
	}
	var located []project
	for _, project := range projects {
		if _, _, err := s.module(project); err != nil {
			// A reactor project without a POM of its own in the checkout
			// (generated, or out of the checkout): nothing to analyse.
			slog.WarnContext(ctx, "a Maven project has no directory in the checkout; it is left out", "repository_id", r.Checkout.RepositoryID, "project", project.Group+":"+project.Name, "error", err)
			s.gap("", "", "project:"+project.Group+":"+project.Name, gapText("Maven project "+project.Group+":"+project.Name+" was left out: "+err.Error()))
			continue
		}
		located = append(located, project)
	}
	for _, project := range located {
		if err = s.project(project); err != nil {
			return bc.BuildContext{}, err
		}
	}
	for _, project := range located {
		if err = s.paths(project); err != nil {
			return bc.BuildContext{}, err
		}
	}
	if s.inventory.RecordCount()+uint64(len(s.checks)) > r.Limits.MaxRecords {
		return bc.BuildContext{}, bc.ErrLimitExceeded
	}
	digest, err := s.inventory.Digest()
	if err != nil {
		return bc.BuildContext{}, err
	}
	result, err := bc.Seal(bc.BuildContext{RepositoryID: r.Checkout.RepositoryID, SnapshotID: r.Checkout.SnapshotID, Producer: bc.Producer{Name: "maven-resolved", Version: Version, InputSHA256: digest}, Inventory: s.inventory, Checks: s.checks})
	if err != nil {
		return bc.BuildContext{}, err
	}
	body, err := json.Marshal(result)
	if err != nil {
		return bc.BuildContext{}, err
	}
	if uint64(len(body)) > r.Limits.MaxOutputBytes || uint64(len(result.Diagnostics)) > r.Limits.MaxDiagnostics {
		return bc.BuildContext{}, bc.ErrLimitExceeded
	}
	return result, ctx.Err()
}

type observer struct {
	ctx           context.Context
	request       bc.Request
	config        Config
	cache         *manifest.FingerprintCache
	failures      []compileError
	stageFailures []stageFailure
	// duplicates holds the directories of coordinates more than one
	// checkout POM declares; the effective model decides between them.
	duplicates map[string][]string
	inventory  bc.Inventory
	checks     []bc.InputCheck
	inputs     map[bc.InputID]bool
	artifacts  map[bc.ArtifactID]bool
	gaps       map[bc.GapID]bool
	// noOutput holds the sets recorded without compiled output.
	noOutput    map[bc.SourceSetID]bool
	projectDirs map[string]string
	sets        map[string]bc.SourceSetID
	major       int
	hashBytes   uint64
	files       uint64
}

func identifier(prefix, value string) string {
	sum := sha256.Sum256([]byte(value))
	return prefix + ":" + hex.EncodeToString(sum[:])
}
func (s *observer) gap(module bc.ModuleID, set bc.SourceSetID, what, reason string) {
	id := bc.GapID(identifier("maven-gap", string(module)+string(set)+what))
	if s.gaps == nil {
		s.gaps = make(map[bc.GapID]bool)
	}
	if s.gaps[id] {
		return
	}
	s.gaps[id] = true
	s.inventory.MissingInputs = append(s.inventory.MissingInputs, bc.MissingInput{ID: id, Requested: what, Reason: reason, ModuleID: module, SourceSetID: set})
	if what == bc.GapCompiledOutput && set != "" {
		if s.noOutput == nil {
			s.noOutput = map[bc.SourceSetID]bool{}
		}
		s.noOutput[set] = true
	}
}

// gapText is text a gap can carry: one line, no control characters, at
// most 2 KB.
func gapText(text string) string {
	text = strings.Join(strings.Fields(strings.Map(func(r rune) rune {
		if unicode.IsControl(r) {
			return ' '
		}
		return r
	}, strings.ToValidUTF8(text, "?"))), " ")
	if len(text) > 2048 {
		cut := 2045
		for cut > 0 && !utf8.RuneStart(text[cut]) {
			cut--
		}
		text = text[:cut] + "..."
	}
	return text
}

// unbuilt says why Maven left a project without output or classpath when a
// step failed in a stage the build went past: the step that failed on it,
// or, when none names it, the first that failed (Maven skips the modules
// that depend on a failed one). Empty when no step failed.
func (s *observer) unbuilt(p project) string {
	var own []string
	for _, f := range s.stageFailures {
		if f.project == p.Name && len(own) < 2 {
			own = append(own, f.line)
		}
	}
	if len(own) > 0 {
		return gapText("Maven could not build it: " + strings.Join(own, "; "))
	}
	if len(s.stageFailures) > 0 {
		return gapText("Maven did not build it after another step failed: " + s.stageFailures[0].line)
	}
	return ""
}

// rejected says why javac left a source set with these checkout-relative
// roots without classes: its first errors, on one line. Empty when it
// reported none in them.
func (s *observer) rejected(roots []string) string {
	var lines []string
	count := 0
	for _, e := range s.failures {
		for _, root := range roots {
			if root == "." || strings.HasPrefix(e.file, root+"/") {
				count++
				if len(lines) < 5 {
					line := strings.Map(func(r rune) rune {
						if unicode.IsControl(r) {
							return ' '
						}
						return r
					}, e.line)
					if len(line) > 300 {
						line = strings.ToValidUTF8(line[:300], "") + "…"
					}
					lines = append(lines, strings.TrimSpace(line))
				}
				break
			}
		}
	}
	if count == 0 {
		return ""
	}
	reason := "Maven could not compile it: " + strings.Join(lines, "; ")
	if count > len(lines) {
		reason += fmt.Sprintf("; and %d more errors", count-len(lines))
	}
	return reason
}

func (s *observer) relative(name string) (string, error) {
	if strings.Contains(name, "${") {
		return "", fmt.Errorf("%w: unresolved Maven path", bc.ErrInvalidInput)
	}
	name = filepath.Clean(name)
	relative, err := filepath.Rel(s.request.Checkout.Path, name)
	if err != nil {
		return "", err
	}
	relative = filepath.ToSlash(relative)
	if !bc.ValidPath(relative) {
		return "", fmt.Errorf("%w: Maven path is outside owned checkout", bc.ErrInvalidInput)
	}
	return relative, nil
}

func (s *observer) projectRelative(dir, name string) (string, error) {
	if strings.TrimSpace(name) == "" {
		return "", fmt.Errorf("%w: empty Maven source root", bc.ErrInvalidInput)
	}
	if !filepath.IsAbs(name) {
		name = filepath.Join(s.request.Checkout.Path, dir, name)
	}
	return s.relative(name)
}

// locateProjects maps each checkout POM's coordinates to its directory.
// POMs in source trees (test fixtures, archetype templates), POMs that do
// not parse and oversized ones are not projects of the build and are passed
// over; coordinates two POMs declare are kept for the effective model to
// decide between.
func (s *observer) locateProjects() error {
	return filepath.WalkDir(s.request.Checkout.Path, func(name string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if err = s.ctx.Err(); err != nil {
			return err
		}
		s.files++
		if s.files > s.request.Limits.MaxFiles {
			return bc.ErrLimitExceeded
		}
		if entry.IsDir() {
			if name != s.request.Checkout.Path && (skippedDir(entry.Name()) || moduleSources(name)) {
				return filepath.SkipDir
			}
			return nil
		}
		if entry.Name() != "pom.xml" || !entry.Type().IsRegular() {
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if uint64(info.Size()) > s.request.Limits.MaxInputBytes {
			return nil
		}
		f, err := os.Open(name)
		if err != nil {
			return err
		}
		var p project
		err = xml.NewDecoder(io.LimitReader(f, int64(s.request.Limits.MaxInputBytes)+1)).Decode(&p)
		f.Close()
		if err != nil {
			slog.DebugContext(s.ctx, "a pom.xml that does not parse is not a project of the build", "path", name, "error", err)
			return nil
		}
		if p.Group == "" {
			p.Group = p.Parent.Group
		}
		key := p.Group + ":" + p.Name
		relative, err := filepath.Rel(s.request.Checkout.Path, filepath.Dir(name))
		if err != nil {
			return err
		}
		relative = filepath.ToSlash(relative)
		if previous, ok := s.projectDirs[key]; ok && previous != relative {
			if s.duplicates == nil {
				s.duplicates = map[string][]string{}
			}
			if len(s.duplicates[key]) == 0 {
				s.duplicates[key] = []string{previous}
			}
			s.duplicates[key] = append(s.duplicates[key], relative)
			return nil
		}
		s.projectDirs[key] = relative
		return nil
	})
}

// effectiveDir finds a project's directory from the absolute paths of its
// effective model, the build directory's parent or the source directory's
// module, among candidates when given, else wherever a pom.xml is there.
func (s *observer) effectiveDir(p project, candidates []string) (string, bool) {
	var guesses []string
	if p.Build.Directory != "" && filepath.IsAbs(p.Build.Directory) {
		guesses = append(guesses, filepath.Dir(p.Build.Directory))
	}
	for _, source := range []string{p.Build.SourceDirectory, p.Build.TestSourceDirectory} {
		for _, suffix := range []string{"/src/main/java", "/src/test/java"} {
			if base, ok := strings.CutSuffix(filepath.ToSlash(source), suffix); ok && filepath.IsAbs(source) {
				guesses = append(guesses, filepath.FromSlash(base))
			}
		}
	}
	for _, guess := range guesses {
		relative, err := s.relative(guess)
		if err != nil {
			if filepath.Clean(guess) == filepath.Clean(s.request.Checkout.Path) {
				relative = "."
			} else {
				continue
			}
		}
		if candidates != nil {
			if slices.Contains(candidates, relative) {
				return relative, true
			}
			continue
		}
		if info, err := os.Stat(filepath.Join(s.request.Checkout.Path, filepath.FromSlash(relative), "pom.xml")); err == nil && info.Mode().IsRegular() {
			return relative, true
		}
	}
	return "", false
}
func (s *observer) addInput(id bc.InputID, kind bc.InputKind, root, name string) (bc.InputID, error) {
	if s.inputs[id] {
		return id, nil
	}
	s.inputs[id] = true
	actualRoot := s.request.Checkout.Path
	if root == "java-jdk" {
		actualRoot = s.config.JavaHome
	} else if root == "java-cache" {
		actualRoot = s.config.CacheDir
	}
	unavailable := func(reason string) (bc.InputID, error) {
		s.inventory.Inputs = append(s.inventory.Inputs, bc.Input{ID: id, Kind: kind, UnavailableReason: reason})
		s.checks = append(s.checks, bc.InputCheck{InputID: id, Status: bc.Missing})
		return id, nil
	}
	if err := s.budgetInput(actualRoot, name, kind); errors.Is(err, bc.ErrLimitExceeded) && s.ctx.Err() == nil {
		return unavailable("Maven input exceeds the build's size budget")
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		return id, err
	}
	digest, err := manifest.FingerprintCached(s.ctx, s.cache, actualRoot, name, kind, s.request.Limits)
	if errors.Is(err, os.ErrNotExist) {
		return unavailable("Maven declared input was not materialized")
	}
	if errors.Is(err, bc.ErrLimitExceeded) && s.ctx.Err() == nil {
		return unavailable("Maven input exceeds the build's size budget")
	}
	if err != nil {
		return id, err
	}
	s.inventory.Inputs = append(s.inventory.Inputs, bc.Input{ID: id, Kind: kind, Location: &bc.Location{Root: root, Path: name}, SHA256: digest})
	s.checks = append(s.checks, bc.InputCheck{InputID: id, Status: bc.Available, ObservedSHA256: digest})
	return id, nil
}

// jdkRelease is what a JDK's release file says about it.
type jdkRelease struct {
	version string
	vendor  string
	major   int
}

func readJDK(home string) (jdkRelease, error) {
	f, err := os.Open(filepath.Join(home, "release"))
	if err != nil {
		return jdkRelease{}, err
	}
	defer f.Close()
	values := map[string]string{}
	scan := bufio.NewScanner(f)
	for scan.Scan() {
		key, value, ok := strings.Cut(scan.Text(), "=")
		if ok {
			values[key] = strings.Trim(value, "\"")
		}
	}
	if err = scan.Err(); err != nil {
		return jdkRelease{}, err
	}
	version := values["JAVA_VERSION"]
	majorText := strings.Split(version, ".")[0]
	if majorText == "1" {
		parts := strings.Split(version, ".")
		if len(parts) > 1 {
			majorText = parts[1]
		}
	}
	major, err := strconv.Atoi(majorText)
	if err != nil || major < 8 {
		return jdkRelease{}, bc.ErrInvalidInput
	}
	vendor := values["IMPLEMENTOR"]
	if vendor == "" {
		vendor = "JDK release metadata"
	}
	return jdkRelease{version: version, vendor: vendor, major: major}, nil
}

// jdkMajor reads a JDK's major version from its release file, or asks its
// launcher when there is none; some JDK 8 builds, such as Corretto's for
// macOS, ship without one.
func jdkMajor(home string) (int, error) {
	release, err := readJDK(home)
	if err == nil {
		return release.major, nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return 0, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, filepath.Join(home, "bin", "java"), "-XshowSettings:properties", "-version").CombinedOutput()
	if err != nil {
		return 0, fmt.Errorf("%s -version: %w", home, err)
	}
	for _, line := range strings.Split(string(out), "\n") {
		key, value, ok := strings.Cut(strings.TrimSpace(line), " = ")
		if ok && key == "java.specification.version" {
			if major, ok := javaLevel(value); ok && major >= 8 {
				return major, nil
			}
		}
	}
	return 0, fmt.Errorf("%w: %s reports no Java version", bc.ErrInvalidInput, home)
}

func (s *observer) jdk() error {
	release, err := readJDK(s.config.JavaHome)
	if err != nil {
		return err
	}
	s.major = release.major
	id, err := s.addInput("maven-jdk", bc.InputJDK, "java-jdk", ".")
	if err != nil {
		return err
	}
	s.inventory.JDKs = append(s.inventory.JDKs, bc.JDK{ID: "maven-jdk", HomeInputID: id, Vendor: release.vendor, Version: release.version, Major: s.major})
	return nil
}
func (s *observer) module(p project) (bc.ModuleID, string, error) {
	key := p.Group + ":" + p.Name
	id := bc.ModuleID(identifier("maven-module", key))
	if candidates := s.duplicates[key]; len(candidates) > 0 {
		if dir, ok := s.effectiveDir(p, candidates); ok {
			return id, dir, nil
		}
		return "", "", fmt.Errorf("%w: several checkout POMs declare %s", bc.ErrIdentityMismatch, key)
	}
	dir, ok := s.projectDirs[key]
	if !ok {
		if dir, ok = s.effectiveDir(p, nil); !ok {
			return "", "", fmt.Errorf("%w: effective project has no exact checkout POM: %s", bc.ErrIdentityMismatch, p.Name)
		}
	}
	return id, dir, nil
}
func (s *observer) project(p project) error {
	mid, dir, err := s.module(p)
	if err != nil {
		return err
	}
	s.inventory.Modules = append(s.inventory.Modules, bc.Module{ID: mid, Name: p.Name, Directory: dir, Coordinates: &bc.Coordinates{Group: p.Group, Name: p.Name, Version: p.Version, Extension: p.Packaging}})
	if p.Packaging == "" {
		s.inventory.Modules[len(s.inventory.Modules)-1].Coordinates.Extension = "jar"
	}
	for _, test := range []bool{false, true} {
		name, kind, rootName, outputName := "main", bc.SourceSetMain, p.Build.SourceDirectory, p.Build.OutputDirectory
		if test {
			name, kind, rootName, outputName = "test", bc.SourceSetTest, p.Build.TestSourceDirectory, p.Build.TestOutputDirectory
		}
		sid := bc.SourceSetID(string(mid) + ":" + name)
		set := bc.SourceSet{ID: sid, ModuleID: mid, Name: name, Kind: kind, Language: "java", JDKID: "maven-jdk"}
		var compiler configuration
		var generated []string
		// Checkout-relative roots, for the compile errors of this set.
		var roots []string
		for _, plug := range p.Build.Plugins {
			if plug.Name == "maven-compiler-plugin" {
				compiler = plug.Configuration
				for _, execution := range plug.Executions {
					if (!test && execution.ID == "default-compile") || (test && execution.ID == "default-testCompile") {
						compiler = mergeConfiguration(compiler, execution.Configuration)
					}
				}
			}
			if plug.Name == "build-helper-maven-plugin" {
				for _, execution := range plug.Executions {
					for _, goal := range execution.Goals {
						if (!test && goal == "add-source") || (test && goal == "add-test-source") {
							generated = append(generated, execution.Configuration.Sources...)
						}
					}
				}
			}
			// Executed source generators materialize below Maven's standard generated
			// roots. Observe every immediate language directory in deterministic order.
		}
		if rootName != "" {
			relative, err := s.relative(rootName)
			if err != nil {
				// Outside the checkout, or a property Maven left unresolved.
				s.gap(mid, sid, "source_root", gapText("Its source directory "+rootName+" is not in the checkout; its sources were left out."))
				relative = ""
			}
			if relative == "" {
			} else if info, e := os.Stat(filepath.Join(s.request.Checkout.Path, relative)); e == nil && info.IsDir() {
				id, err := s.addInput(bc.InputID(identifier("source", relative)), bc.InputSourceRoot, "checkout", relative)
				if err != nil {
					return err
				}
				set.SourceRootIDs = append(set.SourceRootIDs, id)
				roots = append(roots, relative)
			} else if e != nil && !errors.Is(e, os.ErrNotExist) {
				return e
			}
		}
		generatedBase := filepath.Join(s.request.Checkout.Path, dir, "target", "generated-sources")
		if test {
			generatedBase = filepath.Join(s.request.Checkout.Path, dir, "target", "generated-test-sources")
		}
		if entries, e := os.ReadDir(generatedBase); e == nil {
			for _, entry := range entries {
				if entry.IsDir() {
					candidate := filepath.Join(generatedBase, entry.Name())
					exists, err := s.hasJava(candidate)
					if err != nil {
						return err
					}
					if exists {
						generated = append(generated, candidate)
					}
				}
			}
		} else if !errors.Is(e, os.ErrNotExist) {
			return e
		}
		kept := generated[:0]
		for _, name := range generated {
			relative, err := s.projectRelative(dir, name)
			if err != nil {
				s.gap(mid, sid, "generated_root:"+name, gapText("Its generated source directory "+name+" is not in the checkout; its sources were left out."))
				continue
			}
			kept = append(kept, relative)
		}
		generated = kept
		sort.Strings(generated)
		previous := ""
		for _, relative := range generated {
			if relative == previous {
				continue
			}
			previous = relative
			roots = append(roots, relative)
			// build-helper also adds ordinary source roots. A module directory
			// (or its ancestor) contains build outputs and bookkeeping alongside
			// sources; fingerprinting that entire tree makes retries depend on
			// checkout paths and packaging timestamps. Discovery hashes each
			// selected source file, as it does for the primary source root.
			if relative == dir || relative == "." || strings.HasPrefix(dir, relative+"/") {
				id, err := s.addInput(bc.InputID(identifier("source", relative)), bc.InputSourceRoot, "checkout", relative)
				if err != nil {
					return err
				}
				if !containsInput(set.SourceRootIDs, id) {
					set.SourceRootIDs = append(set.SourceRootIDs, id)
				}
				continue
			}
			if containsInput(set.SourceRootIDs, bc.InputID(identifier("source", relative))) {
				continue
			}
			id, err := s.addInput(bc.InputID(identifier("generated", relative)), bc.InputGeneratedRoot, "checkout", relative)
			if err != nil {
				return err
			}
			set.GeneratedRootIDs = append(set.GeneratedRootIDs, id)
		}
		if len(set.SourceRootIDs)+len(set.GeneratedRootIDs) == 0 {
			continue
		}
		if !s.compilerSettings(p, &set, compiler, test) {
			continue
		}
		if rejected := s.rejected(roots); rejected != "" {
			// javac wrote no classes for this set; whatever the directory
			// holds is not its compiled output.
			s.gap(mid, sid, bc.GapCompiledOutput, rejected)
		} else if outputName != "" {
			relative, err := s.relative(outputName)
			if err != nil {
				relative = ""
			}
			if info, e := os.Stat(filepath.Join(s.request.Checkout.Path, relative)); relative != "" && e == nil && info.IsDir() {
				id, err := s.addInput(bc.InputID(identifier("classes", relative)), bc.InputClasses, "checkout", relative)
				if err != nil {
					return err
				}
				set.OutputInputID = id
			} else if !test {
				reason := s.unbuilt(p)
				if reason == "" {
					reason = "Maven main compilation output is unavailable"
				}
				s.gap(mid, sid, bc.GapCompiledOutput, reason)
			}
		}
		s.sets[p.Group+":"+p.Name+":"+p.Version+":"+name] = sid
		s.inventory.SourceSets = append(s.inventory.SourceSets, set)
	}
	return s.compilerExecutions(p, mid)
}

func containsInput(ids []bc.InputID, id bc.InputID) bool {
	for _, existing := range ids {
		if existing == id {
			return true
		}
	}
	return false
}

func (s *observer) hasJava(root string) (bool, error) {
	found := false
	err := filepath.WalkDir(root, func(name string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if err = s.ctx.Err(); err != nil {
			return err
		}
		s.files++
		if s.files > s.request.Limits.MaxFiles {
			return bc.ErrLimitExceeded
		}
		relative, e := filepath.Rel(root, name)
		if e != nil {
			return e
		}
		if uint32(strings.Count(filepath.ToSlash(relative), "/")+1) > s.request.Limits.MaxDepth {
			return bc.ErrLimitExceeded
		}
		if !entry.IsDir() && strings.HasSuffix(entry.Name(), ".java") {
			found = true
			return filepath.SkipAll
		}
		return nil
	})
	return found, err
}
func (s *observer) budgetInput(root, name string, kind bc.InputKind) error {
	full := filepath.Join(root, name)
	return filepath.WalkDir(full, func(name string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if err = s.ctx.Err(); err != nil {
			return err
		}
		s.files++
		if s.files > s.request.Limits.MaxFiles {
			return bc.ErrLimitExceeded
		}
		if kind == bc.InputSourceRoot {
			if entry.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		relative, e := filepath.Rel(full, name)
		if e != nil {
			return e
		}
		if uint32(strings.Count(filepath.ToSlash(relative), "/")+1) > s.request.Limits.MaxDepth {
			return bc.ErrLimitExceeded
		}
		if entry.Type().IsRegular() {
			info, e := entry.Info()
			if e != nil {
				return e
			}
			if uint64(info.Size()) > s.request.Limits.MaxHashBytes-s.hashBytes {
				return bc.ErrLimitExceeded
			}
			s.hashBytes += uint64(info.Size())
		}
		return nil
	})
}

func (s *observer) paths(p project) error {
	mid, dir, err := s.module(p)
	if err != nil {
		return err
	}
	for i := range s.inventory.SourceSets {
		set := &s.inventory.SourceSets[i]
		if set.ModuleID != mid {
			continue
		}
		name := "compile"
		if set.Kind == bc.SourceSetTest {
			name = "test"
			if main, ok := s.sets[p.Group+":"+p.Name+":"+p.Version+":main"]; ok {
				// Also without main output (its gap says why): resolution
				// compiles main for the tests, or leaves lookups into it
				// unresolved.
				if s.hasOutput(main) || s.noOutput[main] {
					set.Classpath = append(set.Classpath, bc.PathEntry{Kind: bc.EntrySourceSet, RefID: string(main)})
				}
			} else if p.Build.OutputDirectory != "" {
				// A module may compile another language without a Java main
				// source set. Its Java tests still see the actual main output.
				relative, err := s.relative(p.Build.OutputDirectory)
				if err != nil {
					relative = "."
				}
				info, err := os.Stat(filepath.Join(s.request.Checkout.Path, relative))
				if err != nil && !errors.Is(err, os.ErrNotExist) {
					return err
				}
				if err == nil && info.IsDir() && relative != "." {
					aid := bc.ArtifactID(identifier("maven-main-classes", relative))
					input, err := s.addInput(bc.InputID(aid), bc.InputClasses, "checkout", relative)
					if err != nil {
						return err
					}
					s.inventory.Artifacts = append(s.inventory.Artifacts, bc.Artifact{ID: aid, Coordinates: bc.Coordinates{Group: p.Group, Name: p.Name, Version: p.Version, Extension: "jar"}, BinaryInputID: input})
					s.artifacts[aid] = true
					set.Classpath = append(set.Classpath, bc.PathEntry{Kind: bc.EntryArtifact, RefID: string(aid)})
				}
			}
		}
		file := filepath.Join(s.request.Checkout.Path, dir, "target", "codegraph-"+name+"-classpath.txt")
		info, err := os.Stat(file)
		if err == nil && uint64(info.Size()) > s.request.Limits.MaxInputBytes {
			s.gap(mid, set.ID, "classpath:"+name, "Its dependency classpath is longer than the build's input limit")
			continue
		}
		var body []byte
		if err == nil {
			body, err = os.ReadFile(file)
		}
		if errors.Is(err, os.ErrNotExist) {
			reason := s.unbuilt(p)
			if reason == "" {
				reason = "Effective ordered dependency classpath was not observed"
			}
			s.gap(mid, set.ID, "classpath:"+name, reason)
			continue
		}
		if err != nil {
			return err
		}
		for _, entry := range filepath.SplitList(strings.TrimSpace(string(body))) {
			if entry == "" {
				continue
			}
			pathEntry, ok, err := s.classpathEntry(entry)
			if err != nil {
				return err
			}
			if ok {
				set.Classpath = append(set.Classpath, pathEntry)
			}
		}
	}
	return nil
}

// classpathEntry places one entry of a Maven classpath: a JAR of the
// isolated repository, or a JAR or class directory in the checkout (a
// system-scoped dependency, or a reactor module's classes). Anything else is
// left out, recorded once per file name: a JAR elsewhere on the machine is
// not a pinned input, and a POM, ZIP or WAR dependency is nothing javac
// reads. ok is false for an entry left out.
func (s *observer) classpathEntry(name string) (bc.PathEntry, bool, error) {
	repo := filepath.Join(s.config.CacheDir, "repository")
	if relative, err := filepath.Rel(repo, name); err == nil && bc.ValidPath(filepath.ToSlash(relative)) {
		if !strings.HasSuffix(name, ".jar") {
			return bc.PathEntry{}, false, nil
		}
		return s.jar(name, filepath.ToSlash(relative))
	}
	if relative, err := s.relative(name); err == nil {
		info, err := os.Stat(filepath.Join(s.request.Checkout.Path, filepath.FromSlash(relative)))
		if err != nil {
			if errors.Is(err, os.ErrNotExist) {
				return bc.PathEntry{}, false, nil
			}
			return bc.PathEntry{}, false, err
		}
		kind, prefix := bc.InputJAR, "maven-checkout-jar"
		switch {
		case info.IsDir():
			for _, set := range s.inventory.SourceSets {
				if set.OutputInputID == bc.InputID(identifier("classes", relative)) {
					return bc.PathEntry{Kind: bc.EntrySourceSet, RefID: string(set.ID)}, true, nil
				}
			}
			kind, prefix = bc.InputClasses, "maven-checkout-classes"
		case !info.Mode().IsRegular() || !strings.HasSuffix(relative, ".jar"):
			return bc.PathEntry{}, false, nil
		}
		aid := bc.ArtifactID(identifier(prefix, relative))
		if !s.artifacts[aid] {
			s.artifacts[aid] = true
			input, err := s.addInput(bc.InputID(aid), kind, "checkout", relative)
			if err != nil {
				return bc.PathEntry{}, false, err
			}
			base := strings.TrimSuffix(path.Base(relative), ".jar")
			s.inventory.Artifacts = append(s.inventory.Artifacts, bc.Artifact{ID: aid, Coordinates: bc.Coordinates{Group: "checkout", Name: base, Version: "0", Extension: "jar"}, BinaryInputID: input})
		}
		return bc.PathEntry{Kind: bc.EntryArtifact, RefID: string(aid)}, true, nil
	}
	if strings.HasSuffix(name, ".jar") {
		base := filepath.Base(name)
		s.gap("", "", "classpath_entry:"+base, gapText("The classpath entry "+base+" is outside the checkout and the Maven repository and was left out."))
	}
	return bc.PathEntry{}, false, nil
}

// hasOutput reports whether a source set observed so far pins compiled output.
func (s *observer) hasOutput(id bc.SourceSetID) bool {
	for _, set := range s.inventory.SourceSets {
		if set.ID == id {
			return set.OutputInputID != ""
		}
	}
	return false
}

// jar places a JAR of the isolated repository, at relative, by the
// coordinates its repository path spells.
func (s *observer) jar(name, relative string) (bc.PathEntry, bool, error) {
	parts := strings.Split(relative, "/")
	if len(parts) < 4 {
		return bc.PathEntry{}, false, nil
	}
	group := strings.Join(parts[:len(parts)-3], ".")
	artifactName, version := parts[len(parts)-3], parts[len(parts)-2]
	filename := parts[len(parts)-1]
	classifier := ""
	stem := strings.TrimSuffix(filename, ".jar")
	base := artifactName + "-" + version
	if stem != base {
		// A timestamped snapshot's name does not spell its directory's
		// version, and names no classifier either.
		if c, ok := strings.CutPrefix(stem, base+"-"); ok {
			classifier = c
		}
	}
	key := ""
	if classifier == "" {
		key = group + ":" + artifactName + ":" + version + ":main"
	} else if classifier == "tests" {
		key = group + ":" + artifactName + ":" + version + ":test"
	}
	if sid, ok := s.sets[key]; ok {
		// A classifier can exist without a materialized source-set output,
		// particularly when test compilation is disabled. Preserve the actual
		// pinned JAR instead of inventing source output visibility from its GAV.
		for _, set := range s.inventory.SourceSets {
			if set.ID == sid && set.OutputInputID != "" {
				return bc.PathEntry{Kind: bc.EntrySourceSet, RefID: string(sid)}, true, nil
			}
			// A module javac rejected installed an empty JAR; its source
			// set is what dependents see, for resolution to compile.
			if set.ID == sid && classifier == "" && s.noOutput[sid] {
				return bc.PathEntry{Kind: bc.EntrySourceSet, RefID: string(sid)}, true, nil
			}
			if set.ID == sid && classifier == "tests" {
				s.gap(set.ModuleID, set.ID, "consumed_test_output", "Required reactor test-helper classes were not materialized; dependency JAR bytes alone do not establish the current checkout output")
			}
		}
	}
	aid := bc.ArtifactID(identifier("maven-artifact", relative))
	if !s.artifacts[aid] {
		immutablePath, err := s.retainJAR(name)
		if errors.Is(err, bc.ErrLimitExceeded) || errors.Is(err, os.ErrNotExist) {
			// Over the build's byte budget, or gone: left out, and said so
			// once per file name.
			s.gap("", "", "classpath_entry:"+filename, gapText("The dependency "+filename+" was left out: "+err.Error()))
			return bc.PathEntry{}, false, nil
		}
		if err != nil {
			return bc.PathEntry{}, false, err
		}
		s.artifacts[aid] = true
		input, err := s.addInput(bc.InputID(aid), bc.InputJAR, "java-cache", immutablePath)
		if err != nil {
			return bc.PathEntry{}, false, err
		}
		s.inventory.Artifacts = append(s.inventory.Artifacts, bc.Artifact{ID: aid, Coordinates: bc.Coordinates{Group: group, Name: artifactName, Version: version, Classifier: classifier, Extension: "jar"}, BinaryInputID: input})
	}
	return bc.PathEntry{Kind: bc.EntryArtifact, RefID: string(aid)}, true, nil
}

// Maven may overwrite a locally installed GAV in a later run. Compiler inputs
// therefore point to independent immutable content objects, not mutable m2 paths.
// A JAR whose digest is remembered and whose object already exists is neither
// read nor copied; otherwise it is read exactly once, hashing while copying,
// and both the repository path and the object are remembered.
func (s *observer) retainJAR(name string) (string, error) {
	info, err := os.Stat(name)
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() || uint64(info.Size()) > s.request.Limits.MaxHashBytes-s.hashBytes {
		return "", bc.ErrLimitExceeded
	}
	s.hashBytes += uint64(info.Size())
	dir := filepath.Join(s.config.CacheDir, "objects")
	if err = os.MkdirAll(dir, 0700); err != nil {
		return "", err
	}
	if digest, ok := s.cache.Lookup(name, info); ok {
		if existing, e := os.Lstat(filepath.Join(dir, digest+".jar")); e == nil && existing.Mode().IsRegular() && existing.Size() == info.Size() {
			return filepath.ToSlash(filepath.Join("objects", digest+".jar")), nil
		}
	}
	input, err := os.Open(name)
	if err != nil {
		return "", err
	}
	defer input.Close()
	output, err := os.CreateTemp(dir, "jar-*")
	if err != nil {
		return "", err
	}
	defer os.Remove(output.Name())
	defer output.Close()
	hash := sha256.New()
	reader := &contextInput{ctx: s.ctx, reader: input}
	size, err := io.Copy(io.MultiWriter(output, hash), io.LimitReader(reader, info.Size()+1))
	if err != nil {
		return "", err
	}
	if size != info.Size() {
		return "", bc.ErrIdentityMismatch
	}
	if err = output.Sync(); err != nil {
		return "", err
	}
	if err = output.Close(); err != nil {
		return "", err
	}
	digest := hex.EncodeToString(hash.Sum(nil))
	relative := filepath.ToSlash(filepath.Join("objects", digest+".jar"))
	target := filepath.Join(s.config.CacheDir, relative)
	linkErr := os.Link(output.Name(), target)
	if linkErr != nil && !errors.Is(linkErr, os.ErrExist) {
		return "", linkErr
	}
	if after, e := input.Stat(); e == nil && after.Size() == info.Size() && after.ModTime().Equal(info.ModTime()) {
		s.cache.Remember(name, info, digest)
	}
	if linkErr == nil {
		if linked, e := os.Lstat(target); e == nil {
			s.cache.Remember(target, linked, digest)
		}
	}
	return relative, nil
}

type contextInput struct {
	ctx    context.Context
	reader io.Reader
}

func (r *contextInput) Read(b []byte) (int, error) {
	if err := r.ctx.Err(); err != nil {
		return 0, err
	}
	return r.reader.Read(b)
}

// skippedDir names directories that never hold a Maven project of the
// checkout: VCS data, build output and installed JavaScript or Python
// packages, which can hold more files than the rest of the checkout.
func skippedDir(name string) bool {
	switch name {
	case ".git", "target", "node_modules", "bower_components", ".gradle", ".idea", ".venv", "venv", "__pycache__", ".tox", ".next", ".nuxt":
		return true
	}
	return false
}

// hasPOM reports whether a pom.xml exists anywhere in the checkout outside
// the directories skippedDir names, the same places locateProjects reads,
// within the request's file budget.
func hasPOM(ctx context.Context, r bc.Request) (bool, error) {
	var files uint64
	found := false
	err := filepath.WalkDir(r.Checkout.Path, func(name string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		files++
		if files > r.Limits.MaxFiles {
			return bc.ErrLimitExceeded
		}
		if entry.IsDir() {
			if name != r.Checkout.Path && skippedDir(entry.Name()) {
				return filepath.SkipDir
			}
			return nil
		}
		if entry.Name() == "pom.xml" {
			found = true
			return filepath.SkipAll
		}
		return nil
	})
	return found, err
}

// moduleSources reports whether a directory is a Maven module's source
// tree: a src directory beside a pom.xml. A POM inside one is a test fixture
// or an archetype template, not a project of the build.
func moduleSources(dir string) bool {
	if filepath.Base(dir) != "src" {
		return false
	}
	info, err := os.Stat(filepath.Join(filepath.Dir(dir), "pom.xml"))
	return err == nil && info.Mode().IsRegular()
}

// The aggregator project rootPOM writes for a checkout with several Maven
// projects; readProjects leaves it out of the model.
const (
	aggregatorGroup    = "codegraph.internal"
	aggregatorArtifact = "codegraph-aggregator"
)

// rootPOM chooses the POM Maven builds. A checkout with a pom.xml at its
// root builds that (""). Otherwise the top-level projects are the
// directories with a pom.xml that no other such directory contains: one is
// built with -f, and several are listed as modules of an aggregator POM
// written in the scratch directory, so one reactor builds them all.
func rootPOM(ctx context.Context, r bc.Request, scratch string) (string, error) {
	if info, err := os.Stat(filepath.Join(r.Checkout.Path, "pom.xml")); err == nil && info.Mode().IsRegular() {
		return "", nil
	}
	var dirs []string
	var files uint64
	err := filepath.WalkDir(r.Checkout.Path, func(name string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		files++
		if files > r.Limits.MaxFiles {
			return bc.ErrLimitExceeded
		}
		if !entry.IsDir() || name == r.Checkout.Path {
			return nil
		}
		if skippedDir(entry.Name()) {
			return filepath.SkipDir
		}
		if info, err := os.Stat(filepath.Join(name, "pom.xml")); err == nil && info.Mode().IsRegular() {
			relative, err := filepath.Rel(r.Checkout.Path, name)
			if err != nil {
				return err
			}
			dirs = append(dirs, filepath.ToSlash(relative))
			// A project's modules are its subdirectories.
			return filepath.SkipDir
		}
		return nil
	})
	if err != nil || len(dirs) == 0 {
		return "", err
	}
	sort.Strings(dirs)
	if len(dirs) == 1 {
		return filepath.Join(dirs[0], "pom.xml"), nil
	}
	slog.InfoContext(ctx, "the checkout holds several Maven projects; one reactor builds them all", "repository_id", r.Checkout.RepositoryID, "projects", dirs)
	aggregator := filepath.Join(scratch, "aggregator")
	if err = os.MkdirAll(aggregator, 0700); err != nil {
		return "", err
	}
	// Relative module paths between physical directories: a symlinked
	// temporary directory would otherwise resolve ".." elsewhere.
	from, err := filepath.EvalSymlinks(aggregator)
	if err != nil {
		return "", err
	}
	checkout, err := filepath.EvalSymlinks(r.Checkout.Path)
	if err != nil {
		return "", err
	}
	var b strings.Builder
	fmt.Fprintf(&b, "<project xmlns=\"http://maven.apache.org/POM/4.0.0\">\n  <modelVersion>4.0.0</modelVersion>\n  <groupId>%s</groupId>\n  <artifactId>%s</artifactId>\n  <version>0</version>\n  <packaging>pom</packaging>\n  <modules>\n", aggregatorGroup, aggregatorArtifact)
	for _, dir := range dirs {
		module, err := filepath.Rel(from, filepath.Join(checkout, filepath.FromSlash(dir)))
		if err != nil {
			return "", err
		}
		var escaped strings.Builder
		if err = xml.EscapeText(&escaped, []byte(filepath.ToSlash(module))); err != nil {
			return "", err
		}
		fmt.Fprintf(&b, "    <module>%s</module>\n", escaped.String())
	}
	b.WriteString("  </modules>\n</project>\n")
	pom := filepath.Join(aggregator, "pom.xml")
	return pom, os.WriteFile(pom, []byte(b.String()), 0600)
}
