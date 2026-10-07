package java

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"
)

// job is the JSON document handed to the bridge. Every path is service-written.
type job struct {
	Root           string       `json:"root"`
	Parallelism    int          `json:"parallelism"`
	ThreadStackMiB int          `json:"thread_stack_mib"`
	ContextSeconds int64        `json:"context_seconds"`
	Contexts       []jobContext `json:"contexts"`
}

type jobContext struct {
	ID         string `json:"id"`
	Sources    string `json:"sources"`
	Classpath  string `json:"classpath"`
	Sourcepath string `json:"sourcepath"`
	Release    string `json:"release"`
	Mode       string `json:"mode"`
	Target     string `json:"target"`
	Events     string `json:"events"`
	// Processorpath lists the Lombok JARs to try, in order; empty runs no
	// annotation processor.
	Processorpath []string `json:"processorpath,omitempty"`
	// Generate is the directory for the context's class files; empty writes
	// none.
	Generate string `json:"generate,omitempty"`
}

// ensureBridge compiles BindingBridge.java once per bridge source digest into
// <CacheDir>/bridge/<sha256>/ and returns that directory. A class file that
// already exists is reused; concurrent resolvers race benignly on a rename.
func (r *Resolver) ensureBridge(ctx context.Context) (string, error) {
	r.bridgeOnce.Lock()
	defer r.bridgeOnce.Unlock()
	if r.bridgeDir != "" {
		return r.bridgeDir, nil
	}
	sum := sha256.Sum256(bridgeSource)
	dir := filepath.Join(r.config.CacheDir, "bridge", hex.EncodeToString(sum[:]))
	if info, err := os.Stat(filepath.Join(dir, "BindingBridge.class")); err == nil && info.Mode().IsRegular() {
		r.bridgeDir = dir
		return dir, nil
	}
	if err := os.MkdirAll(filepath.Dir(dir), 0o700); err != nil {
		return "", err
	}
	staging, err := os.MkdirTemp(filepath.Dir(dir), "staging-")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(staging)
	source := filepath.Join(staging, "BindingBridge.java")
	if err := os.WriteFile(source, bridgeSource, 0o600); err != nil {
		return "", err
	}
	cmd := exec.CommandContext(ctx, filepath.Join(r.config.JavaHome, "bin", "javac"), "-proc:none", "-Xlint:none", "-encoding", "UTF-8", "-d", staging, source)
	cmd.Env = cleanEnvironment()
	if out, err := cmd.CombinedOutput(); err != nil {
		return "", fmt.Errorf("compile Java binding bridge: %w: %s", err, strings.TrimSpace(string(out)))
	}
	if err := os.Remove(source); err != nil {
		return "", err
	}
	if err := os.Rename(staging, dir); err != nil {
		if info, statErr := os.Stat(filepath.Join(dir, "BindingBridge.class")); statErr == nil && info.Mode().IsRegular() {
			r.bridgeDir = dir
			return dir, nil
		}
		return "", err
	}
	r.bridgeDir = dir
	return dir, nil
}

// JVM launch options from an ambient user or repository environment cannot
// add agents/processors or otherwise change the configured analysis authority.
func cleanEnvironment() []string {
	var env []string
	for _, v := range os.Environ() {
		key, _, _ := strings.Cut(v, "=")
		if key != "JAVA_TOOL_OPTIONS" && key != "JDK_JAVA_OPTIONS" && key != "_JAVA_OPTIONS" && key != "CLASSPATH" {
			env = append(env, v)
		}
	}
	return env
}

// runBridge attributes the contexts in one JVM and processes each finished
// context in job order while the JVM works on the rest. A context the JVM
// could not finish (it reported context_failed, gave no receipt because the
// JVM died or ran out of time, or broke the protocol) runs once more alone in
// a fresh JVM; if that fails too, its files are processed without compiler
// events: their declarations are still symbols, their lookups unsupported
// with the reason, and the run gets a warning. Only workspace errors and
// cancellation fail the run.
func (s *run) runBridge(bridge string, live []*contextJob) error {
	next, err := s.launch(bridge, live, s.r.config.Parallelism)
	if err != nil {
		return err
	}
	for _, c := range live[next:] {
		if c.done && c.failure == "" {
			continue
		}
		first := c.failure
		if first == "" {
			first = "the compiler process ended before it finished this source set"
		}
		slog.WarnContext(s.ctx, "Java attribution of a source set failed; retrying it alone in a fresh compiler", "source_set", c.set.ID, "reason", first)
		c.reset()
		if _, err := s.launch(bridge, []*contextJob{c}, 1); err != nil {
			return err
		}
		if !c.done {
			c.done, c.failure = true, first
		}
	}
	for _, c := range live[next:] {
		if err := s.processContext(c); err != nil {
			return err
		}
	}
	return nil
}

// launch runs one java process for the jobs and processes the finished ones
// in job order until the first that failed or is unfinished, whose index it
// returns. The process gets a deadline from the contexts' budget; a
// process that dies, misbehaves or runs out of time leaves its unfinished
// contexts undone for the caller.
func (s *run) launch(bridge string, jobs []*contextJob, parallelism int) (int, error) {
	budget := s.r.config.ContextTimeout
	j := job{Root: s.dir, Parallelism: parallelism, ThreadStackMiB: threadStackMiB, ContextSeconds: int64(budget / time.Second)}
	byID := map[string]*contextJob{}
	for _, c := range jobs {
		byID[string(c.set.ID)] = c
		j.Contexts = append(j.Contexts, jobContext{ID: string(c.set.ID), Sources: c.listPath, Classpath: strings.Join(c.classpath, string(os.PathListSeparator)), Sourcepath: strings.Join(c.sourcepath, string(os.PathListSeparator)), Release: fmt.Sprint(c.release), Mode: c.mode, Target: fmt.Sprint(c.target), Events: c.eventsPath, Processorpath: c.processors, Generate: c.generate})
		slog.InfoContext(s.ctx, "Java attribution started", "source_set", c.set.ID, "source_files", len(c.files))
	}
	body, err := json.Marshal(j)
	if err != nil {
		return 0, err
	}
	// One Resolve can launch the bridge more than once (sets compiled for
	// their class files come first, failed contexts run again); each launch
	// has its own files.
	s.launches++
	jobPath := filepath.Join(s.dir, fmt.Sprintf("job-%d.json", s.launches))
	if err := os.WriteFile(jobPath, body, 0o600); err != nil {
		return 0, err
	}
	// The contexts' budget, in rounds of parallel contexts, plus the JVM's
	// own start: javac checks the budget only between compilation phases,
	// so one pathological class is bounded here.
	rounds := (len(jobs) + parallelism - 1) / parallelism
	ctx, cancel := context.WithTimeout(s.ctx, time.Duration(rounds)*budget+2*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(ctx, filepath.Join(s.r.config.JavaHome, "bin", "java"), fmt.Sprintf("-Xmx%dm", s.r.config.MaxHeapMiB), "-Xshare:auto", "-cp", bridge, "BindingBridge", jobPath)
	cmd.Dir = s.dir
	cmd.Env = cleanEnvironment()
	stderrPath := filepath.Join(s.dir, fmt.Sprintf("bridge-%d.stderr", s.launches))
	stderr, err := os.OpenFile(stderrPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return 0, err
	}
	defer stderr.Close()
	cmd.Stderr = stderr
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return 0, err
	}
	if err := cmd.Start(); err != nil {
		return 0, err
	}
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 64<<10), 16<<20)
	var processErr error
	next := 0 // index into jobs of the next context to process, in job order
	for scanner.Scan() {
		line := scanner.Bytes()
		var e event
		if err := json.Unmarshal(line, &e); err != nil || e.Kind == "" {
			// An annotation processor may print to stdout; frames are the
			// JSON objects with a kind.
			slog.WarnContext(s.ctx, "ignored compiler output that is not a control frame", "line", truncate(string(line), 256))
			continue
		}
		c, ok := byID[e.Context]
		switch {
		case !ok || c.done:
			slog.WarnContext(s.ctx, "ignored a compiler control frame for no unfinished context", "kind", e.Kind, "context", e.Context)
		case e.Kind == "context_failed":
			c.done, c.failure = true, "the compiler failed: "+e.Error
		case e.Kind != "context_done":
			slog.WarnContext(s.ctx, "ignored a compiler control frame of unknown kind", "kind", e.Kind, "context", e.Context)
		case e.SourceFiles+uint64(len(e.Excluded))+uint64(c.withheld) != uint64(len(c.files)):
			c.done, c.failure = true, fmt.Sprintf("the compiler covered %d of %d files", e.SourceFiles+uint64(len(e.Excluded))+uint64(c.withheld), len(c.files))
		default:
			c.done, c.errors, c.millis, c.generated, c.leftOut = true, e.Errors, e.Millis, e.ClassesWritten == "true", e.ClassesLeftOut
			if c.excluded == nil {
				c.excluded = map[string]string{}
			}
			for _, entry := range e.Excluded {
				path, reason, _ := strings.Cut(entry, "\t")
				c.excluded[path] = reason
			}
			if len(e.Excluded) > 0 {
				slog.WarnContext(s.ctx, "Java files javac could not compile were left out", "source_set", c.set.ID, "files", e.Excluded)
			}
			if len(e.SkippedClasspath) > 0 {
				slog.WarnContext(s.ctx, "Java classpath entries the compiler could not open were left out", "source_set", c.set.ID, "entries", e.SkippedClasspath)
			}
			if len(e.ProcessorFailures) > 0 {
				slog.WarnContext(s.ctx, "Lombok JARs that could not run on the service JDK were skipped", "source_set", c.set.ID, "failures", e.ProcessorFailures, "processor", e.Processor)
			}
			if len(c.processors) > 0 && e.Processor == "" {
				s.lombokFailed(c, e.ProcessorFailures)
			}
			slog.InfoContext(s.ctx, "Java attribution completed", "source_set", c.set.ID, "source_files", e.SourceFiles, "errors", e.Errors, "seconds", float64(e.Millis)/1000, "compiler", e.Compiler)
		}
		for processErr == nil && next < len(jobs) && jobs[next].done && jobs[next].failure == "" {
			if processErr = s.processContext(jobs[next]); processErr == nil {
				next++
			}
		}
		if processErr != nil {
			break
		}
	}
	if processErr != nil || scanner.Err() != nil {
		_ = cmd.Process.Kill()
	}
	waitErr := cmd.Wait()
	if err := s.ctx.Err(); err != nil {
		return next, err
	}
	if processErr != nil {
		return next, processErr
	}
	if waitErr != nil || ctx.Err() != nil {
		_ = stderr.Close()
		tail, _ := os.ReadFile(stderrPath)
		if len(tail) > 2048 {
			tail = tail[len(tail)-2048:]
		}
		slog.WarnContext(s.ctx, "the compiler process ended abnormally", "error", waitErr, "deadline", ctx.Err() != nil, "stderr", strings.TrimSpace(string(tail)))
	}
	return next, nil
}

// threadStackMiB sizes each context's thread in the bridge: javac and the
// walk recurse as deep as the code nests, and generated code nests
// thousands deep.
const threadStackMiB = 512

// truncate cuts s to at most n bytes on a character boundary.
func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	for n > 0 && !utf8.RuneStart(s[n]) {
		n--
	}
	return s[:n]
}
