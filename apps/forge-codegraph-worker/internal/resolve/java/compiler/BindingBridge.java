/* The ingestion service's Java compiler bridge. One JVM attributes every
 * compilation context of a job with a fresh JavacTask per context. It never
 * generates classes, discovers annotation processors, or runs application or
 * test code. The only processor it runs is Lombok, from a JAR the service
 * names per context, because Lombok adds the members sources call. It writes
 * class files only for a context the service names a directory for (one the
 * build left without output, which other contexts need on their classpath),
 * and only when it compiled without errors. Each
 * context writes its events as JSONL to its own file and reports completion on
 * stdout so the caller can consume finished contexts while later ones are
 * still being attributed. */
import com.sun.source.tree.*;
import com.sun.source.util.*;
import javax.lang.model.element.*;
import javax.lang.model.type.*;
import javax.lang.model.util.*;
import javax.tools.*;
import java.io.*;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;

class BindingBridge {
    private static final Object STDOUT = new Object();

    // The single argument is a service-written JSON job file, never a shell or
    // repository argument: {"root":..., "parallelism":N, "contexts":[{"id","sources","classpath","sourcepath","release","mode","target","events","processorpath","generate"}]}.
    // processorpath lists Lombok JARs to try in order; empty runs no processor.
    // generate is a directory for the context's class files; empty writes none.
    // thread_stack_mib sizes each context's thread (javac recurses as deep as
    // the code nests; generated code nests thousands deep); context_seconds
    // bounds a context's attribution, checked between compilation phases.
    public static void main(String[] args) throws Exception {
        if (args.length != 1) throw new IllegalArgumentException("one service-owned job file argument required");
        Object parsed = new Json(new String(Files.readAllBytes(Paths.get(args[0])), StandardCharsets.UTF_8)).parse();
        if (!(parsed instanceof Map)) throw new IllegalArgumentException("job file must be a JSON object");
        Map<?, ?> job = (Map<?, ?>) parsed;
        Path root = Paths.get(String.valueOf(job.get("root"))).toRealPath();
        int parallelism = job.get("parallelism") instanceof Number ? ((Number) job.get("parallelism")).intValue() : 1;
        if (parallelism < 1) parallelism = 1;
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) throw new IllegalStateException("a JDK compiler is required");
        List<?> specs = job.get("contexts") instanceof List ? (List<?>) job.get("contexts") : Collections.emptyList();
        final long stack = (job.get("thread_stack_mib") instanceof Number ? ((Number) job.get("thread_stack_mib")).longValue() : 512L) << 20;
        final long budget = job.get("context_seconds") instanceof Number ? ((Number) job.get("context_seconds")).longValue() : 1800L;
        ExecutorService pool = Executors.newFixedThreadPool(parallelism, r -> {
            Thread t = new Thread(null, r, "context", stack);
            t.setDaemon(true);
            return t;
        });
        List<Future<Boolean>> futures = new ArrayList<>();
        for (Object spec : specs) {
            if (!(spec instanceof Map)) throw new IllegalArgumentException("context must be a JSON object");
            final Map<?, ?> context = (Map<?, ?>) spec;
            futures.add(pool.submit(() -> {
                String id = String.valueOf(context.get("id"));
                try {
                    new Context(root, compiler, context, budget).run();
                    return Boolean.TRUE;
                } catch (Throwable t) {
                    Map<String, Object> failure = event("context_failed");
                    failure.put("context", id);
                    failure.put("error", failureText(t));
                    stdout(json(failure));
                    return Boolean.FALSE;
                }
            }));
        }
        for (Future<Boolean> f : futures) f.get();
        pool.shutdown();
        // A context that failed said so on stdout; the job itself ran.
        System.exit(0);
    }

    /* The innermost cause of a failure, as "class: first message line". */
    static String failureText(Throwable t) {
        Throwable cause = rootCause(t);
        String message = String.valueOf(cause.getMessage()).split("\n", 2)[0];
        return cause.getClass().getName() + (cause.getMessage() == null ? "" : ": " + message);
    }
    static Throwable rootCause(Throwable t) {
        Throwable cause = t;
        while (cause.getCause() != null && cause.getCause() != cause) cause = cause.getCause();
        return cause;
    }
    private static void stdout(String line) { synchronized (STDOUT) { System.out.println(line); System.out.flush(); } }

    /* A classpath JAR this JDK's zip file system refuses (newer JDKs reject
     * zip64 fields some old JARs carry) makes javac fail nearly every lookup
     * in the context, not only those into that JAR. Such entries are left out
     * and reported; lookups into them stay unresolved. Each path is opened at
     * most once per job. */
    private static final Map<String, String> UNREADABLE = new ConcurrentHashMap<>();
    private static final Set<String> READABLE = ConcurrentHashMap.newKeySet();

    private static String readableClasspath(String classpath, List<String> skipped) {
        if (classpath.isEmpty()) return classpath;
        StringJoiner kept = new StringJoiner(File.pathSeparator);
        for (String entry : classpath.split(java.util.regex.Pattern.quote(File.pathSeparator), -1)) {
            String reason = entry.isEmpty() ? null : unreadable(entry);
            if (reason == null) kept.add(entry);
            else skipped.add(entry + ": " + reason);
        }
        return kept.toString();
    }

    private static String unreadable(String entry) {
        if (READABLE.contains(entry)) return null;
        String known = UNREADABLE.get(entry);
        if (known != null) return known;
        Path path = Paths.get(entry);
        // Directories are class trees, and javac ignores missing entries.
        if (!Files.isRegularFile(path)) { READABLE.add(entry); return null; }
        try (FileSystem zip = FileSystems.newFileSystem(path, (ClassLoader) null)) {
            READABLE.add(entry);
            return null;
        } catch (IOException | RuntimeException e) {
            String reason = e.getClass().getSimpleName() + ": " + e.getMessage();
            UNREADABLE.put(entry, reason);
            return reason;
        }
    }

    /* The processor could not run on this JDK, so the context is attributed
     * again with the next candidate, or with none. */
    private static final class ProcessorFailure extends Exception {
        ProcessorFailure(String message) { super(message); }
    }

    /* javac crashed on one file (a stack overflow on deeply nested generated
     * code, or a compiler bug): the context is attributed again without it. */
    private static final class UnitFailure extends Exception {
        final String path;
        UnitFailure(String path, String message) { super(message); this.path = path; }
    }

    /* The bridge's own walk of a unit overflowed; raised inside javac's
     * callbacks, where only unchecked exceptions pass. */
    private static final class UnitCrash extends RuntimeException {
        final String path;
        UnitCrash(String path, Throwable cause) { super(failureText(cause)); this.path = path; }
    }

    /* The context used up its time or diagnostic budget: it fails as a whole. */
    private static final class ContextBudget extends RuntimeException {
        ContextBudget(String message) { super(message); }
    }

    /* At most this many files are left out of one context before it fails. */
    private static final int MAX_EXCLUDED = 8;

    /* One compilation context: its own JavacTask, file manager, position maps
     * and event writer. Nothing here is shared with another context. */
    private static final class Context {
        private final Path root;
        private final JavaCompiler compiler;
        private final Map<?, ?> spec;
        private final String id;
        private long errors, warnings, declarations, bindings, unresolved;
        private StandardJavaFileManager files;
        private Trees trees;
        private Types types;
        private Elements elements;
        private Writer out;
        // The events of the unit being walked, while it is walked.
        private List<Map<String, Object>> held;
        private final Set<Tree> inferredVariables = Collections.newSetFromMap(new IdentityHashMap<Tree, Boolean>());
        private final Map<String, List<Map<String, Object>>> pendingDiagnostics = new LinkedHashMap<>();
        private final Map<URI, int[]> offsets = new LinkedHashMap<URI, int[]>(16, .75f, true) {
            protected boolean removeEldestEntry(Map.Entry<URI, int[]> entry) { return size() > 4; }
        };
        /* With a processor, every tree the sources had before it ran; null
         * without one. A declaration outside it was generated by Lombok, and
         * code outside it is never reported: it is not in any source file. */
        private Set<Tree> original;
        // Why the processor disabled itself, when it said so.
        private String processorDisabled;
        // Whether the context's class files were written to generate.
        private boolean generated;
        // Files left out of the class files written for a context with
        // errors, root-relative, and the files javac reported errors in.
        private final Set<String> leftOut = new TreeSet<>();
        private final Set<String> errorPaths = new HashSet<>();
        // The file javac is parsing or attributing, for a crash to name.
        private String current;
        // Files left out of the context, by root-relative path, with why:
        // javac crashed on them, or the binding walk could not finish them.
        private final Map<String, String> excluded = new LinkedHashMap<>();
        private int unwalked;
        private final long budgetNanos;
        private long deadline;

        Context(Path root, JavaCompiler compiler, Map<?, ?> spec, long budgetSeconds) {
            this.root = root; this.compiler = compiler; this.spec = spec; this.id = String.valueOf(spec.get("id"));
            this.budgetNanos = budgetSeconds * 1_000_000_000L;
        }
        private String text(String key) { Object v = spec.get(key); return v == null ? "" : String.valueOf(v); }

        void run() throws Exception {
            long started = System.nanoTime();
            List<String> skipped = new ArrayList<>();
            String classpath = readableClasspath(text("classpath"), skipped);
            List<String> processors = new ArrayList<>();
            if (spec.get("processorpath") instanceof List) for (Object p : (List<?>) spec.get("processorpath")) if (p != null && !String.valueOf(p).isEmpty()) processors.add(String.valueOf(p));
            // Each Lombok JAR in turn; one that cannot run on this JDK leaves
            // the context to the next, and the last attempt runs none.
            List<String> failures = new ArrayList<>();
            String processor = null;
            int units = -1;
            deadline = started + budgetNanos;
            for (int i = 0; units < 0; ) {
                processor = i < processors.size() ? processors.get(i) : null;
                try {
                    units = attribute(classpath, processor);
                } catch (ProcessorFailure f) {
                    failures.add(processor + ": " + f.getMessage());
                    i++;
                } catch (UnitFailure u) {
                    // One file crashed javac: leave it out, with its sources
                    // out of reach of the source path, and compile the rest.
                    if (excluded.size() >= MAX_EXCLUDED || excluded.containsKey(u.path)) throw new IllegalStateException("javac crashed on " + u.path + ": " + u.getMessage());
                    excluded.put(u.path, "javac crashed on it: " + u.getMessage());
                    Path file = root.resolve(u.path);
                    if (Files.exists(file)) Files.move(file, file.resolveSibling(file.getFileName() + ".excluded"), StandardCopyOption.REPLACE_EXISTING);
                }
            }
            Map<String, Object> done = event("context_done");
            done.put("context", id); done.put("errors", errors); done.put("warnings", warnings); done.put("source_files", units - unwalked);
            if (!excluded.isEmpty()) {
                List<String> list = new ArrayList<>();
                for (Map.Entry<String, String> e : excluded.entrySet()) list.add(e.getKey() + "\t" + e.getValue());
                done.put("excluded", list);
            }
            done.put("millis", (System.nanoTime() - started) / 1000000L); done.put("compiler", System.getProperty("java.runtime.version"));
            if (!skipped.isEmpty()) done.put("skipped_classpath", skipped);
            if (processor != null) done.put("processor", processor);
            if (!text("generate").isEmpty()) done.put("classes_written", generated);
            if (generated && !leftOut.isEmpty()) done.put("classes_left_out", new ArrayList<>(leftOut));
            if (!failures.isEmpty()) done.put("processor_failures", failures);
            stdout(json(done));
        }

        /* Attributes the context with one processor JAR, or none, and writes
         * its events. Returns the number of compilation units. */
        private int attribute(String classpath, String processor) throws Exception {
            errors = 0; warnings = 0; declarations = 0; bindings = 0; unresolved = 0; generated = false; current = null; unwalked = 0;
            leftOut.clear(); errorPaths.clear();
            inferredVariables.clear(); pendingDiagnostics.clear(); offsets.clear();
            original = processor == null ? null : Collections.newSetFromMap(new IdentityHashMap<Tree, Boolean>());
            processorDisabled = null;
            DiagnosticListener<JavaFileObject> listener = d -> {
                if (d.getKind() == Diagnostic.Kind.ERROR) { errors++; if (d.getSource() != null) errorPaths.add(path(d.getSource().toUri())); }
                if (d.getKind() == Diagnostic.Kind.WARNING || d.getKind() == Diagnostic.Kind.MANDATORY_WARNING) warnings++;
                // A context flooded with errors (a classpath that could not be
                // built) keeps compiling; only the first diagnostics are kept.
                if (errors + warnings > 100000) return;
                String message = d.getMessage(Locale.ROOT);
                // Lombok on a javac it does not support says so and stops.
                if (processor != null && d.getCode() != null && d.getCode().endsWith("proc.messager") && message.contains("lombok will not work")) processorDisabled = message.split("\n", 2)[0];
                Map<String, Object> event = event("diagnostic");
                event.put("code", d.getCode()); event.put("severity", d.getKind().toString());
                event.put("message", message);
                String path = "";
                if (d.getSource() != null) { path = path(d.getSource().toUri()); event.put("path", path); event.put("start", byteOffset(d.getSource(), d.getStartPosition())); event.put("end", byteOffset(d.getSource(), d.getEndPosition())); }
                // Diagnostics arrive during attribution, before any unit is
                // walked. They are held and written with their unit so every
                // file's events stay contiguous in the stream.
                pendingDiagnostics.computeIfAbsent(path, k -> new ArrayList<>()).add(event);
            };
            files = compiler.getStandardFileManager(listener, Locale.ROOT, StandardCharsets.UTF_8);
            List<String> inputs = new ArrayList<>();
            for (String input : Files.readAllLines(Paths.get(text("sources")), StandardCharsets.UTF_8)) {
                String relative = relative(input);
                if (excluded.containsKey(relative)) continue;
                // An earlier attempt of this context left it out.
                if (!Files.exists(Paths.get(input))) { excluded.put(relative, "left out by an earlier attempt: javac crashed on it"); continue; }
                inputs.add(input);
            }
            List<String> options = new ArrayList<>(Arrays.asList("-implicit:none", "-Xprefer:source", "-encoding", "UTF-8", "-Xlint:none", "-Xmaxerrs", "100000", "-classpath", classpath, "-sourcepath", text("sourcepath")));
            // A processor path is the only place processors are looked for.
            if (processor == null) options.add("-proc:none");
            else options.addAll(Arrays.asList("-processorpath", processor));
            if (!text("generate").isEmpty()) options.addAll(Arrays.asList("-d", text("generate")));
            if (text("mode").equals("source-target")) options.addAll(Arrays.asList("-source", text("release"), "-target", text("target")));
            else options.addAll(Arrays.asList("--release", text("release")));
            JavacTask task = (JavacTask) compiler.getTask(null, files, listener, options, null, files.getJavaFileObjectsFromStrings(inputs));
            trees = Trees.instance(task); types = task.getTypes(); elements = task.getElements();
            // Units are entered, with javac's own implicit members, before the
            // first processing round; Lombok adds its members after. Later
            // rounds enter the units again, so only the first counts.
            final Set<CompilationUnitTree> entered = Collections.newSetFromMap(new IdentityHashMap<CompilationUnitTree, Boolean>());
            task.addTaskListener(new TaskListener() {
                public void started(TaskEvent e) {
                    if (System.nanoTime() > deadline) throw new ContextBudget("the context's time budget ran out");
                    // The file a crash in this phase belongs to.
                    if (e.getKind() == TaskEvent.Kind.PARSE && e.getSourceFile() != null) current = path(e.getSourceFile().toUri());
                    if (e.getKind() == TaskEvent.Kind.ANALYZE && e.getCompilationUnit() != null) current = path(e.getCompilationUnit().getSourceFile().toUri());
                }
                public void finished(TaskEvent e) {
                    if (e.getKind() == TaskEvent.Kind.PARSE || e.getKind() == TaskEvent.Kind.ANALYZE) current = null;
                    if (original == null || e.getKind() != TaskEvent.Kind.ENTER || e.getCompilationUnit() == null || !entered.add(e.getCompilationUnit())) return;
                    try {
                        new TreeScanner<Void, Void>() {
                            public Void scan(Tree t, Void v) { if (t != null) original.add(t); return super.scan(t, v); }
                        }.scan(e.getCompilationUnit(), null);
                    } catch (StackOverflowError overflow) {
                        throw new UnitCrash(path(e.getCompilationUnit().getSourceFile().toUri()), overflow);
                    }
                }
            });
            List<CompilationUnitTree> units = new ArrayList<>();
            try {
                for (CompilationUnitTree unit : task.parse()) units.add(unit);
            } catch (RuntimeException | Error e) {
                files.close();
                throw failure(e, processor);
            }
            // Attribution replaces inferred variable types. Remember the original
            // trees and join their attributed types through the declaration span.
            for (CompilationUnitTree unit : units) {
                try {
                    new TreeScanner<Void, Void>() {
                        public Void visitVariable(VariableTree t, Void v) {
                            if (t.getType() == null) inferredVariables.add(t);
                            return super.visitVariable(t, v);
                        }
                    }.scan(unit, null);
                } catch (StackOverflowError overflow) {
                    files.close();
                    throw new UnitFailure(path(unit.getSourceFile().toUri()), failureText(overflow));
                }
            }
            // analyze() performs attribution/flow only; generate()/call() are never used.
            try {
                task.analyze();
            } catch (RuntimeException | Error e) {
                files.close();
                throw failure(e, processor);
            }
            if (processorDisabled != null) {
                files.close();
                throw new ProcessorFailure(processorDisabled);
            }
            try (Writer writer = new BufferedWriter(new OutputStreamWriter(new FileOutputStream(text("events")), StandardCharsets.UTF_8), 1 << 20)) {
                out = writer;
                // Reading an element can attribute a tree javac left alone
                // (with a processor it leaves more), which reports errors for
                // the unit being walked. A unit's events are held until its
                // walk ends, so those errors are written with it, first.
                Set<String> written = new HashSet<>();
                for (CompilationUnitTree unit : units) {
                    String path = path(unit.getSourceFile().toUri());
                    held = new ArrayList<>();
                    try {
                        new Scanner(unit).scan(unit, null);
                    } catch (StackOverflowError | RuntimeException e) {
                        // Its bindings are lost, not the context's: the file
                        // is reported as left out, with its diagnostics only.
                        held = new ArrayList<>();
                        excluded.put(path, "the binding walk could not finish it: " + failureText(e));
                        unwalked++;
                    }
                    List<Map<String, Object>> events = held;
                    held = null;
                    flushDiagnostics(path);
                    for (Map<String, Object> event : events) emit(event);
                    written.add(path);
                }
                // Every file's events stay contiguous: an error reported for
                // a unit after it was written is left out.
                for (String path : new ArrayList<>(pendingDiagnostics.keySet())) if (!written.contains(path)) flushDiagnostics(path);
                Map<String, Object> summary = event("summary");
                summary.put("errors", errors); summary.put("warnings", warnings); summary.put("declarations", declarations); summary.put("bindings", bindings); summary.put("unresolved", unresolved);
                summary.put("compiler", System.getProperty("java.runtime.version")); summary.put("source_files", units.size());
                emit(summary);
                out.flush();
            }
            // Class files for the contexts that need this one's output, once
            // its events are written: code generation lowers the trees in
            // place. javac writes none for sources with errors; its
            // dependents then see no output, as without generation.
            if (!text("generate").isEmpty() && errors == 0) {
                try {
                    task.generate();
                    generated = errors == 0;
                } catch (RuntimeException | Error e) {
                    generated = false;
                }
            } else if (!text("generate").isEmpty()) {
                files.close();
                generated = generatePartial(inputs, options);
                return units.size();
            }
            files.close();
            return units.size();
        }
        /* Class files for as much of a context with errors as compiles. javac
         * writes none for a compilation with any error, so the files it
         * reported errors in are left out, then the files that fail without
         * them, until what remains compiles; its dependents see the rest of a
         * module one broken file would otherwise take out whole. The
         * sourcepath is dropped so a file left out is not read back in. */
        private boolean generatePartial(List<String> inputs, List<String> options) {
            List<String> remaining = new ArrayList<>();
            for (String input : inputs) {
                if (errorPaths.contains(path(Paths.get(input).toUri()))) leftOut.add(relative(input)); else remaining.add(input);
            }
            if (leftOut.isEmpty()) return false; // errors outside the sources
            List<String> passOptions = new ArrayList<>();
            for (int i = 0; i < options.size(); i++) {
                if (options.get(i).equals("-sourcepath") && i + 1 < options.size()) { i++; continue; }
                passOptions.add(options.get(i));
            }
            for (int pass = 0; pass < 6 && !remaining.isEmpty(); pass++) {
                if (System.nanoTime() > deadline) break;
                Set<String> failing = new HashSet<>();
                int[] count = {0};
                DiagnosticListener<JavaFileObject> listener = d -> { if (d.getKind() == Diagnostic.Kind.ERROR) { count[0]++; if (d.getSource() != null) failing.add(path(d.getSource().toUri())); } };
                try (StandardJavaFileManager passFiles = compiler.getStandardFileManager(listener, Locale.ROOT, StandardCharsets.UTF_8)) {
                    JavacTask pass2 = (JavacTask) compiler.getTask(null, passFiles, listener, passOptions, null, passFiles.getJavaFileObjectsFromStrings(remaining));
                    if (Boolean.TRUE.equals(pass2.call()) && count[0] == 0) return true;
                } catch (RuntimeException | Error | IOException e) {
                    return false;
                }
                List<String> next = new ArrayList<>();
                for (String input : remaining) {
                    if (failing.contains(path(Paths.get(input).toUri()))) leftOut.add(relative(input)); else next.add(input);
                }
                if (next.size() == remaining.size()) return false; // errors it cannot pin to a file
                remaining = next;
            }
            return false;
        }
        /* What a crash in parse or analyze means: the context's budget ran out
         * or the heap did (the context fails); the processor could not run
         * (the next one is tried); or javac crashed on the file it was on
         * (that file is left out). */
        private Exception failure(Throwable e, String processor) {
            for (Throwable t = e; t != null; t = t.getCause() == t ? null : t.getCause()) if (t instanceof UnitCrash) return new UnitFailure(((UnitCrash) t).path, t.getMessage());
            Throwable cause = rootCause(e);
            if (cause instanceof ContextBudget) return (ContextBudget) cause;
            if (cause instanceof OutOfMemoryError) return new IllegalStateException(failureText(cause));
            if (current != null) return new UnitFailure(current, failureText(cause));
            if (processor != null) return new ProcessorFailure(failureText(cause));
            return new IllegalStateException(failureText(cause));
        }
        /* A materialized input's root-relative path, as path() reports it. */
        private String relative(String input) {
            Path p = Paths.get(input).toAbsolutePath().normalize();
            return p.startsWith(root) ? root.relativize(p).toString().replace(File.separatorChar, '/') : p.toString();
        }
        private void flushDiagnostics(String path) {
            List<Map<String, Object>> list = pendingDiagnostics.remove(path);
            if (list == null) return;
            for (Map<String, Object> event : list) emit(event);
        }

        private final class Scanner extends TreePathScanner<Void, Void> {
            private final CompilationUnitTree unit;
            Scanner(CompilationUnitTree unit) { this.unit = unit; }
            // Code a processor added is in no source file; its declarations
            // are reported only as the targets of source references.
            public Void scan(Tree t, Void v) { return t != null && original != null && !original.contains(t) ? null : super.scan(t, v); }
            public Void visitClass(ClassTree t, Void v) { declaration(t); return super.visitClass(t, v); }
            public Void visitMethod(MethodTree t, Void v) { declaration(t); overrides(t); return super.visitMethod(t, v); }
            public Void visitVariable(VariableTree t, Void v) { declaration(t); return super.visitVariable(t, v); }
            public Void visitTypeParameter(TypeParameterTree t, Void v) { declaration(t); return super.visitTypeParameter(t, v); }
            public Void visitIdentifier(IdentifierTree t, Void v) { binding(t); return super.visitIdentifier(t, v); }
            public Void visitMemberSelect(MemberSelectTree t, Void v) { binding(t); return super.visitMemberSelect(t, v); }
            public Void visitMethodInvocation(MethodInvocationTree t, Void v) { binding(t); return super.visitMethodInvocation(t, v); }
            public Void visitNewClass(NewClassTree t, Void v) { binding(t); return super.visitNewClass(t, v); }
            public Void visitMemberReference(MemberReferenceTree t, Void v) { binding(t); implementsEvent(t); return super.visitMemberReference(t, v); }
            public Void visitLambdaExpression(LambdaExpressionTree t, Void v) { implementsEvent(t); return super.visitLambdaExpression(t, v); }
            public Void visitAnnotatedType(AnnotatedTypeTree t, Void v) { binding(t); return super.visitAnnotatedType(t, v); }
            public Void visitPrimitiveType(PrimitiveTypeTree t, Void v) { binding(t); return super.visitPrimitiveType(t, v); }
            public Void visitParameterizedType(ParameterizedTypeTree t, Void v) { binding(t); return super.visitParameterizedType(t, v); }
            public Void visitNewArray(NewArrayTree t, Void v) { binding(t); return super.visitNewArray(t, v); }
            public Void visitArrayType(ArrayTypeTree t, Void v) { binding(t); return super.visitArrayType(t, v); }
            public Void visitWildcard(WildcardTree t, Void v) { binding(t); return super.visitWildcard(t, v); }
            public Void visitUnionType(UnionTypeTree t, Void v) { binding(t); return super.visitUnionType(t, v); }
            public Void visitIntersectionType(IntersectionTypeTree t, Void v) { binding(t); return super.visitIntersectionType(t, v); }
            private void declaration(Tree t) {
                Element e = trees.getElement(getCurrentPath()); if (e == null) return;
                Map<String, Object> event = located("declaration", unit, t); if (event == null) return;
                describe(event, e);
                if (e instanceof TypeElement) event.put("nesting", ((TypeElement) e).getNestingKind().toString());
                event.put("signature_valid", Boolean.toString(validType(e.asType(), newSeen())));
                if (inferredVariables.contains(t)) event.put("inferred_type", inferredType(e.asType(), 0));
                declarations++; emit(event);
            }
            // Every method B that M overrides, found by walking all supertypes of
            // the enclosing type once. Javac decides overriding; nothing is
            // inferred from names or signatures here.
            private void overrides(MethodTree t) {
                Element e = trees.getElement(getCurrentPath());
                if (!(e instanceof ExecutableElement) || e.getKind() != ElementKind.METHOD) return;
                ExecutableElement m = (ExecutableElement) e;
                if (!(m.getEnclosingElement() instanceof TypeElement)) return;
                TypeElement type = (TypeElement) m.getEnclosingElement();
                if (m.asType() == null || !validType(m.asType(), newSeen())) return;
                Set<Element> seen = new HashSet<>();
                Deque<TypeMirror> queue = new ArrayDeque<>(types.directSupertypes(type.asType()));
                List<ExecutableElement> overridden = new ArrayList<>();
                while (!queue.isEmpty()) {
                    TypeMirror s = queue.poll();
                    if (!(s instanceof DeclaredType)) continue;
                    Element se = ((DeclaredType) s).asElement();
                    if (!(se instanceof TypeElement) || !seen.add(se)) continue;
                    for (Element member : se.getEnclosedElements()) {
                        if (member.getKind() == ElementKind.METHOD && member instanceof ExecutableElement && member.getSimpleName().contentEquals(m.getSimpleName()) && elements.overrides(m, (ExecutableElement) member, type)) overridden.add((ExecutableElement) member);
                    }
                    queue.addAll(types.directSupertypes(s));
                }
                overridden.sort(Comparator.comparing((ExecutableElement b) -> elementName(b.getEnclosingElement())).thenComparing(BindingBridge.Context.this::signature));
                for (ExecutableElement b : overridden) {
                    Map<String, Object> event = located("override", unit, t); if (event == null) continue;
                    describe(event, b); locate(event, b); emit(event);
                }
            }
            // A lambda or method reference implements the single abstract method
            // of its attributed functional interface target type.
            private void implementsEvent(Tree t) {
                Map<String, Object> event = located("implements", unit, t); if (event == null) return;
                TypeMirror target = trees.getTypeMirror(getCurrentPath());
                if (target instanceof IntersectionType && validType(target, newSeen())) {
                    event.put("status", "unsupported"); event.put("message", "intersection functional interface target type"); emit(event); return;
                }
                if (!(target instanceof DeclaredType) || !validType(target, newSeen())) {
                    event.put("status", "unresolved"); event.put("message", "functional interface target type is not attributed"); unresolved++; emit(event); return;
                }
                Element ie = ((DeclaredType) target).asElement();
                if (!(ie instanceof TypeElement)) { event.put("status", "unresolved"); event.put("message", "functional interface target has no type element"); unresolved++; emit(event); return; }
                TypeElement iface = (TypeElement) ie;
                Map<String, Object> ifaceEvent = event("interface");
                describe(ifaceEvent, iface); locate(ifaceEvent, iface);
                event.put("interface", ifaceEvent);
                List<ExecutableElement> abstracts = new ArrayList<>();
                for (Element member : elements.getAllMembers(iface)) {
                    if (!(member instanceof ExecutableElement) || member.getKind() != ElementKind.METHOD) continue;
                    Set<Modifier> mods = member.getModifiers();
                    if (!mods.contains(Modifier.ABSTRACT) || mods.contains(Modifier.DEFAULT) || mods.contains(Modifier.STATIC)) continue;
                    if (isPublicObjectMethod((ExecutableElement) member)) continue;
                    boolean duplicate = false;
                    for (ExecutableElement prior : abstracts) if (elements.overrides(prior, (ExecutableElement) member, iface) || elements.overrides((ExecutableElement) member, prior, iface) || types.isSubsignature((ExecutableType) prior.asType(), (ExecutableType) member.asType())) { duplicate = true; break; }
                    if (!duplicate) abstracts.add((ExecutableElement) member);
                }
                if (abstracts.size() != 1) {
                    event.put("status", "unsupported"); event.put("message", "functional interface target has " + abstracts.size() + " abstract methods"); emit(event); return;
                }
                ExecutableElement sam = abstracts.get(0);
                describe(event, sam); locate(event, sam); event.put("status", "resolved"); bindings++; emit(event);
            }
            private boolean isPublicObjectMethod(ExecutableElement m) {
                TypeElement object = elements.getTypeElement("java.lang.Object");
                if (object == null) return false;
                for (Element om : object.getEnclosedElements()) {
                    if (om.getKind() == ElementKind.METHOD && om.getModifiers().contains(Modifier.PUBLIC) && om.getSimpleName().contentEquals(m.getSimpleName()) && types.isSubsignature((ExecutableType) m.asType(), (ExecutableType) om.asType())) return true;
                }
                return false;
            }
            private void candidates(Tree tree, Element selected) {
                List<Element> list = candidateList(tree, selected);
                // An overload set this large is not a candidate list worth
                // keeping: the call keeps its binding without alternatives.
                if (list == null || list.size() > 20000) return;
                for (Element m : list) { Map<String, Object> event = locatedBinding("candidate", tree); if (event == null) continue; describe(event, m); locate(event, m); emit(event); }
            }
            // soleCandidate reports whether the selected method is the only
            // accessible one of its name that takes the call's number of
            // arguments. Javac still chooses a method when an argument is
            // erroneous; with overloads that choice is arbitrary, without
            // them it is the only target the call can have.
            private boolean soleCandidate(Tree tree, ExecutableElement selected) {
                int args = tree instanceof MethodInvocationTree ? ((MethodInvocationTree) tree).getArguments().size() : tree instanceof NewClassTree ? ((NewClassTree) tree).getArguments().size() : -1;
                List<Element> list = candidateList(tree, selected);
                if (args < 0 || list == null || !list.contains(selected)) return false;
                int applicable = 0;
                for (Element m : list) {
                    ExecutableElement x = (ExecutableElement) m; int n = x.getParameters().size();
                    if (n == args || (x.isVarArgs() && args >= n - 1)) applicable++;
                }
                int own = selected.getParameters().size();
                return applicable == 1 && (own == args || (selected.isVarArgs() && args >= own - 1));
            }
            private List<Element> candidateList(Tree tree, Element selected) {
                TypeMirror receiver = null;
                if (tree instanceof MethodInvocationTree) { ExpressionTree select = ((MethodInvocationTree) tree).getMethodSelect(); if (select instanceof MemberSelectTree) receiver = trees.getTypeMirror(new TreePath(new TreePath(getCurrentPath(), select), ((MemberSelectTree) select).getExpression())); }
                if (receiver == null && tree instanceof MemberReferenceTree) receiver = trees.getTypeMirror(new TreePath(getCurrentPath(), ((MemberReferenceTree) tree).getQualifierExpression()));
                if (receiver == null && (tree instanceof NewClassTree)) receiver = trees.getTypeMirror(new TreePath(getCurrentPath(), ((NewClassTree) tree).getIdentifier()));
                // Constructor candidates belong to the class being initialized;
                // an explicit super(...) call must not enumerate subclass ctors.
                if (selected.getKind() == ElementKind.CONSTRUCTOR) receiver = selected.getEnclosingElement().asType();
                Scope scope = trees.getScope(getCurrentPath());
                // getScope can expose copied symbols for an anonymous class.
                // Resolve its original lexical ClassTree so candidate members
                // retain Trees source paths and exact declaration provenance.
                if (receiver == null) { for (TreePath p = getCurrentPath(); p != null; p = p.getParentPath()) if (p.getLeaf() instanceof ClassTree) { Element enclosing = trees.getElement(p); if (enclosing instanceof TypeElement) receiver = enclosing.asType(); break; } }
                if (receiver == null && scope.getEnclosingClass() != null) receiver = scope.getEnclosingClass().asType();
                if (!(receiver instanceof DeclaredType)) receiver = selected.getEnclosingElement().asType();
                if (!(receiver instanceof DeclaredType)) return null;
                TypeElement owner = (TypeElement) ((DeclaredType) receiver).asElement();
                List<Element> list = new ArrayList<>();
                List<? extends Element> members = selected.getKind() == ElementKind.CONSTRUCTOR ? owner.getEnclosedElements() : elements.getAllMembers(owner);
                for (Element m : members) if (m instanceof ExecutableElement && m.getSimpleName().contentEquals(selected.getSimpleName()) && trees.isAccessible(scope, m, (DeclaredType) receiver)) list.add(m);
                if (tree instanceof MethodInvocationTree && !(((MethodInvocationTree) tree).getMethodSelect() instanceof MemberSelectTree)) {
                    for (ImportTree imp : unit.getImports()) if (imp.isStatic() && imp.getQualifiedIdentifier() instanceof MemberSelectTree) { MemberSelectTree member = (MemberSelectTree) imp.getQualifiedIdentifier(); if (!member.getIdentifier().contentEquals("*") && !member.getIdentifier().contentEquals(selected.getSimpleName())) continue; TreePath importOwner = TreePath.getPath(unit, member.getExpression()); Element element = importOwner == null ? null : trees.getElement(importOwner); if (element instanceof TypeElement) { TypeElement imported = (TypeElement) element; for (Element m : elements.getAllMembers(imported)) if (m instanceof ExecutableElement && m.getModifiers().contains(Modifier.STATIC) && m.getSimpleName().contentEquals(selected.getSimpleName()) && trees.isAccessible(scope, m, (DeclaredType) imported.asType())) list.add(m); } }
                }
                list.sort(Comparator.comparing(BindingBridge.Context.this::signature).thenComparing(e -> elementName(e.getEnclosingElement())));
                return list;
            }
            // The syntax parser includes the statement terminator on explicit
            // constructor invocations and represents an enum initializer by its
            // variable span. Javac's parent tree supplies those exact positions;
            // attribution still comes from the constructor invocation itself.
            private Map<String, Object> locatedBinding(String kind, Tree t) {
                Tree location = t; TreePath parent = getCurrentPath().getParentPath();
                if (parent != null && t instanceof MethodInvocationTree && parent.getLeaf() instanceof ExpressionStatementTree) {
                    ExpressionTree select = ((MethodInvocationTree) t).getMethodSelect();
                    String name = select instanceof IdentifierTree ? ((IdentifierTree) select).getName().toString() : select instanceof MemberSelectTree ? ((MemberSelectTree) select).getIdentifier().toString() : "";
                    if (name.equals("this") || name.equals("super")) location = parent.getLeaf();
                }
                if (parent != null && t instanceof NewClassTree && parent.getLeaf() instanceof VariableTree) {
                    Element variable = trees.getElement(parent);
                    if (variable != null && variable.getKind() == ElementKind.ENUM_CONSTANT) location = parent.getLeaf();
                }
                Map<String, Object> event = located(kind, unit, location);
                if (event != null) event.put("tree_kind", t.getKind().toString());
                return event;
            }
            private void binding(Tree t) {
                Map<String, Object> event = locatedBinding("binding", t); if (event == null) return;
                if (t instanceof MemberSelectTree) { String n = ((MemberSelectTree) t).getIdentifier().toString(); if (n.equals("class") || n.equals("this") || n.equals("super")) { event.put("status", "language_expression"); emit(event); return; } }
                // Some javac versions leave annotation wrapper trees without a
                // mirror. Their underlying type is attributed at this exact path.
                TreePath bindingPath = getCurrentPath();
                while (bindingPath.getLeaf() instanceof AnnotatedTypeTree) bindingPath = new TreePath(bindingPath, ((AnnotatedTypeTree) bindingPath.getLeaf()).getUnderlyingType());
                TypeMirror mirror = trees.getTypeMirror(bindingPath); Element e = trees.getElement(bindingPath);
                event.put("type", mirror == null ? "" : mirror.toString()); event.put("type_kind", mirror == null ? "NONE" : mirror.getKind().toString());
                if (mirror instanceof ArrayType) { int dimensions = 0; TypeMirror component = mirror; while (component instanceof ArrayType) { dimensions++; component = ((ArrayType) component).getComponentType(); } event.put("array_dimensions", dimensions); }
                boolean errorType = mirror != null && !validType(mirror, newSeen());
                boolean signatureError = e != null && (e.asType() == null || !validType(e.asType(), newSeen()));
                // A call javac attributed whose value is erroneous only because
                // an argument is: the method is the call's target when no other
                // of its name takes that many arguments.
                if (errorType && !signatureError && e instanceof ExecutableElement && (t instanceof MethodInvocationTree || t instanceof NewClassTree) && soleCandidate(t, (ExecutableElement) e)) errorType = false;
                if (signatureError) errorType = true;
                if (e != null && !errorType && e.asType().getKind() != TypeKind.ERROR) {
                    describe(event, e);
                    locate(event, e);
                    boolean arrayConstructor = false;
                    if (t instanceof MemberReferenceTree && ((MemberReferenceTree) t).getMode() == MemberReferenceTree.ReferenceMode.NEW) { TypeMirror qualifier = trees.getTypeMirror(new TreePath(getCurrentPath(), ((MemberReferenceTree) t).getQualifierExpression())); arrayConstructor = qualifier instanceof ArrayType && validType(qualifier, newSeen()); }
                    boolean arrayClone = false;
                    if (e.getKind() == ElementKind.METHOD && e.getSimpleName().contentEquals("clone")) {
                        TypeMirror receiver = null;
                        if (t instanceof MethodInvocationTree) { ExpressionTree select = ((MethodInvocationTree) t).getMethodSelect(); if (select instanceof MemberSelectTree) receiver = trees.getTypeMirror(new TreePath(new TreePath(getCurrentPath(), select), ((MemberSelectTree) select).getExpression())); }
                        if (t instanceof MemberReferenceTree && ((MemberReferenceTree) t).getMode() == MemberReferenceTree.ReferenceMode.INVOKE) receiver = trees.getTypeMirror(new TreePath(getCurrentPath(), ((MemberReferenceTree) t).getQualifierExpression()));
                        arrayClone = receiver instanceof ArrayType && validType(receiver, newSeen());
                    }
                    event.put("status", arrayConstructor ? "array_constructor" : arrayClone ? "array_clone" : "resolved"); if (e instanceof ExecutableElement && !arrayConstructor && !arrayClone) candidates(t, e); bindings++;
                } else if (!errorType && mirror != null && (mirror.getKind().isPrimitive() || mirror.getKind() == TypeKind.VOID)) { event.put("status", "intrinsic"); bindings++; }
                else if (!errorType && mirror != null && (mirror.getKind() == TypeKind.WILDCARD || mirror.getKind() == TypeKind.UNION || mirror.getKind() == TypeKind.INTERSECTION || mirror.getKind() == TypeKind.ARRAY)) { event.put("status", "structural_type"); bindings++; }
                else { event.put("status", "unresolved"); unresolved++; }
                emit(event);
            }
        }
        private static Set<TypeMirror> newSeen() { return Collections.newSetFromMap(new IdentityHashMap<TypeMirror, Boolean>()); }
        private Map<String, Object> located(String kind, CompilationUnitTree unit, Tree t) {
            long start = trees.getSourcePositions().getStartPosition(unit, t), end = trees.getSourcePositions().getEndPosition(unit, t);
            if (start < 0 || end < start) return null;
            Map<String, Object> e = event(kind); e.put("path", path(unit.getSourceFile().toUri())); e.put("start", byteOffset(unit.getSourceFile(), start)); e.put("end", byteOffset(unit.getSourceFile(), end)); e.put("tree_kind", t.getKind().toString()); return e;
        }
        // A target is its source declaration span when javac has a tree for it,
        // otherwise the class-file origin, otherwise the enclosing source type.
        private void locate(Map<String, Object> event, Element e) {
            TreePath target = trees.getPath(e);
            if (target != null) { CompilationUnitTree u = target.getCompilationUnit(); event.put("target_path", path(u.getSourceFile().toUri())); event.put("target_start", byteOffset(u.getSourceFile(), trees.getSourcePositions().getStartPosition(u, target.getLeaf()))); event.put("target_end", byteOffset(u.getSourceFile(), trees.getSourcePositions().getEndPosition(u, target.getLeaf()))); generated(event, e, target); return; }
            String origin = origin(e);
            if (!origin.isEmpty()) event.put("artifact_uri", origin); else sourceOwner(event, e);
        }
        /* Marks a target Lombok generated: its tree was not in the sources and
         * javac did not make it (a default constructor is MANDATED). Its
         * target span is the field or annotation it was generated from;
         * source_owner is the nearest enclosing type written in the sources. */
        private void generated(Map<String, Object> event, Element e, TreePath target) {
            if (original == null || original.contains(target.getLeaf()) || elements.getOrigin(e) != Elements.Origin.EXPLICIT) return;
            event.put("generated", "lombok");
            for (Element owner = e.getEnclosingElement(); owner != null; owner = owner.getEnclosingElement()) {
                if (!(owner instanceof TypeElement)) continue;
                TreePath p = trees.getPath(owner);
                if (p == null || !original.contains(p.getLeaf())) continue;
                CompilationUnitTree u = p.getCompilationUnit();
                event.put("source_owner", elementName(owner));
                event.put("source_owner_path", path(u.getSourceFile().toUri()));
                event.put("source_owner_start", byteOffset(u.getSourceFile(), trees.getSourcePositions().getStartPosition(u, p.getLeaf())));
                event.put("source_owner_end", byteOffset(u.getSourceFile(), trees.getSourcePositions().getEndPosition(u, p.getLeaf())));
                return;
            }
        }
        private void describe(Map<String, Object> event, Element e) {
            event.put("element_kind", e.getKind().toString()); event.put("name", e.getSimpleName().toString());
            Element owner = e.getEnclosingElement(); event.put("owner", owner == null ? "" : elementName(owner)); event.put("signature", signature(e));
            if (owner instanceof TypeElement) { TreePath p = trees.getPath(owner); if (p != null) { CompilationUnitTree u = p.getCompilationUnit(); event.put("owner_source_path", path(u.getSourceFile().toUri())); event.put("owner_source_start", byteOffset(u.getSourceFile(), trees.getSourcePositions().getStartPosition(u, p.getLeaf()))); event.put("owner_source_end", byteOffset(u.getSourceFile(), trees.getSourcePositions().getEndPosition(u, p.getLeaf()))); } }
        }
        private String elementName(Element e) {
            if (e instanceof QualifiedNameable) { String q = ((QualifiedNameable) e).getQualifiedName().toString(); if (q.isEmpty() && e instanceof TypeElement) return elements.getBinaryName((TypeElement) e).toString(); return q; }
            if (e instanceof ExecutableElement) {
                // Initializer locals have a synthetic javac MethodSymbol whose
                // type is null. It is an enclosing execution context, not a
                // declared callable with a parameter signature.
                if (e.getKind() == ElementKind.STATIC_INIT || e.getKind() == ElementKind.INSTANCE_INIT || e.asType() == null) return elementName(e.getEnclosingElement()) + "#" + e.getKind() + ":" + e.getSimpleName();
                return elementName(e.getEnclosingElement()) + "#" + signature(e);
            }
            return e.toString();
        }
        private String signature(Element e) {
            if (e instanceof ExecutableElement) { ExecutableElement m = (ExecutableElement) e; StringJoiner args = new StringJoiner(",", m.getSimpleName() + "(", ")"); for (VariableElement p : m.getParameters()) args.add(erasedTypeName(p.asType())); return args.toString(); }
            if (e instanceof QualifiedNameable) return ((QualifiedNameable) e).getQualifiedName().toString();
            return e.getSimpleName() + ":" + erasedTypeName(e.asType());
        }
        // Type-use annotations are metadata, not part of erased declaration
        // identity. Rendering TypeMirror directly retains them inconsistently
        // between source and class files. Read names and array rank structurally.
        private String erasedTypeName(TypeMirror type) {
            TypeMirror erased = types.erasure(type);
            if (erased instanceof ArrayType) return erasedTypeName(((ArrayType) erased).getComponentType()) + "[]";
            if (erased instanceof DeclaredType) return elementName(((DeclaredType) erased).asElement());
            if (erased.getKind().isPrimitive() || erased.getKind() == TypeKind.VOID) return erased.getKind().name().toLowerCase(Locale.ROOT);
            return erased.toString();
        }
        private Map<String, Object> inferredType(TypeMirror type, int depth) {
            Map<String, Object> event = event("inferred_type");
            event.put("type", type == null ? "" : type.toString());
            event.put("type_kind", type == null ? "NONE" : type.getKind().name());
            if (depth > 64 || type == null || !validType(type, newSeen())) { event.put("status", "unresolved"); return event; }
            if (type.getKind().isPrimitive()) { event.put("type", type.getKind().name().toLowerCase(Locale.ROOT)); event.put("status", "intrinsic"); return event; }
            List<Map<String, Object>> components = new ArrayList<>();
            if (type instanceof ArrayType) { TypeMirror component = type; while (component instanceof ArrayType) component = ((ArrayType) component).getComponentType(); components.add(inferredType(component, depth + 1)); }
            else if (type instanceof IntersectionType) for (TypeMirror bound : ((IntersectionType) type).getBounds()) components.add(inferredType(bound, depth + 1));
            if (!components.isEmpty()) { event.put("status", "structural_type"); event.put("type_components", components); return event; }
            Element element = types.asElement(type);
            if (element == null) { event.put("status", "unresolved"); return event; }
            describe(event, element);
            TreePath target = trees.getPath(element);
            if (target != null) {
                CompilationUnitTree unit = target.getCompilationUnit();
                event.put("target_path", path(unit.getSourceFile().toUri()));
                event.put("target_start", byteOffset(unit.getSourceFile(), trees.getSourcePositions().getStartPosition(unit, target.getLeaf())));
                event.put("target_end", byteOffset(unit.getSourceFile(), trees.getSourcePositions().getEndPosition(unit, target.getLeaf())));
                generated(event, element, target);
            } else { String origin = origin(element); if (!origin.isEmpty()) event.put("artifact_uri", origin); }
            event.put("status", "resolved"); return event;
        }
        private static boolean validType(TypeMirror t, Set<TypeMirror> seen) {
            if (t == null || !seen.add(t)) return true;
            if (t.getKind() == TypeKind.ERROR) return false;
            if (t instanceof TypeVariable) return validType(((TypeVariable) t).getUpperBound(), seen);
            if (t instanceof WildcardType) { WildcardType w = (WildcardType) t; return validType(w.getExtendsBound(), seen) && validType(w.getSuperBound(), seen); }
            if (t instanceof ArrayType) return validType(((ArrayType) t).getComponentType(), seen);
            if (t instanceof ExecutableType) { ExecutableType e = (ExecutableType) t; if (!validType(e.getReturnType(), seen)) return false; for (TypeMirror p : e.getParameterTypes()) if (!validType(p, seen)) return false; for (TypeMirror p : e.getThrownTypes()) if (!validType(p, seen)) return false; }
            if (t instanceof DeclaredType) for (TypeMirror p : ((DeclaredType) t).getTypeArguments()) if (!validType(p, seen)) return false;
            return true;
        }
        private void sourceOwner(Map<String, Object> event, Element member) { Element owner = member.getEnclosingElement(); while (owner != null && !(owner instanceof TypeElement)) owner = owner.getEnclosingElement(); if (owner == null) return; TreePath p = trees.getPath(owner); if (p == null) return; event.put("target_path", path(p.getCompilationUnit().getSourceFile().toUri())); event.put("target_start", -1); event.put("target_end", -1); }
        private String origin(Element e) {
            Element owner = e; while (owner != null && !(owner instanceof TypeElement)) owner = owner.getEnclosingElement(); if (owner == null) return "";
            String binary = elements.getBinaryName((TypeElement) owner).toString();
            try { JavaFileObject object = files.getJavaFileForInput(StandardLocation.CLASS_PATH, binary, JavaFileObject.Kind.CLASS); if (object != null) return object.toUri().toString(); } catch (Exception ignored) {}
            try { JavaFileObject object = files.getJavaFileForInput(StandardLocation.PLATFORM_CLASS_PATH, binary, JavaFileObject.Kind.CLASS); if (object != null) return object.toUri().toString(); } catch (Exception ignored) {}
            try { for (Set<JavaFileManager.Location> locations : files.listLocationsForModules(StandardLocation.SYSTEM_MODULES)) for (JavaFileManager.Location location : locations) { JavaFileObject object = files.getJavaFileForInput(location, binary, JavaFileObject.Kind.CLASS); if (object != null) return object.toUri().toString(); } } catch (Exception ignored) {}
            return "";
        }
        private String path(URI uri) { try { Path p = Paths.get(uri).toRealPath(); if (p.startsWith(root)) return root.relativize(p).toString().replace(File.separatorChar, '/'); return p.toString(); } catch (Exception e) { return uri.toString(); } }
        private long byteOffset(JavaFileObject file, long utf16) {
            if (utf16 < 0) return -1;
            try { int[] map = offsets.get(file.toUri()); if (map == null) { String text = file.getCharContent(true).toString(); map = new int[text.length() + 1]; int bytes = 0; for (int i = 0; i < text.length(); ) { int cp = text.codePointAt(i); int chars = Character.charCount(cp); map[i] = bytes; if (chars == 2) map[i + 1] = bytes; bytes += cp <= 0x7f ? 1 : cp <= 0x7ff ? 2 : cp <= 0xffff ? 3 : 4; i += chars; map[i] = bytes; } offsets.put(file.toUri(), map); } return utf16 < map.length ? map[(int) utf16] : -1; } catch (IOException e) { throw new UncheckedIOException(e); }
        }
        private void emit(Map<String, Object> event) { if (held != null) { held.add(event); return; } try { out.write(json(event)); out.write('\n'); } catch (IOException e) { throw new UncheckedIOException(e); } }
    }

    private static Map<String, Object> event(String kind) { Map<String, Object> m = new LinkedHashMap<>(); m.put("kind", kind); return m; }
    private static String json(Object value) {
        if (value instanceof Map<?, ?>) { StringJoiner j = new StringJoiner(",", "{", "}"); for (Map.Entry<?, ?> e : ((Map<?, ?>) value).entrySet()) j.add(quote(e.getKey().toString()) + ":" + json(e.getValue())); return j.toString(); }
        if (value instanceof Iterable<?>) { StringJoiner j = new StringJoiner(",", "[", "]"); for (Object item : (Iterable<?>) value) j.add(json(item)); return j.toString(); }
        return value instanceof Number ? value.toString() : quote(value.toString());
    }
    private static String quote(String s) { StringBuilder b = new StringBuilder("\""); for (int i = 0; i < s.length(); i++) { char c = s.charAt(i); switch (c) { case '"': b.append("\\\""); break; case '\\': b.append("\\\\"); break; case '\n': b.append("\\n"); break; case '\r': b.append("\\r"); break; case '\t': b.append("\\t"); break; default: if (c < 32) b.append(String.format("\\u%04x", (int) c)); else b.append(c); } } return b.append('"').toString(); }

    /* A minimal JSON reader for the service-written job file: objects, arrays,
     * strings, numbers, true/false/null. The JDK has no public JSON API. */
    private static final class Json {
        private final String text; private int pos;
        Json(String text) { this.text = text; }
        Object parse() { Object v = value(); skipSpace(); if (pos != text.length()) throw error("trailing data"); return v; }
        private Object value() {
            skipSpace();
            if (pos >= text.length()) throw error("unexpected end");
            char c = text.charAt(pos);
            if (c == '{') { pos++; Map<String, Object> m = new LinkedHashMap<>(); skipSpace(); if (peek('}')) { pos++; return m; } while (true) { skipSpace(); String key = string(); skipSpace(); expect(':'); m.put(key, value()); skipSpace(); if (peek(',')) { pos++; continue; } expect('}'); return m; } }
            if (c == '[') { pos++; List<Object> l = new ArrayList<>(); skipSpace(); if (peek(']')) { pos++; return l; } while (true) { l.add(value()); skipSpace(); if (peek(',')) { pos++; continue; } expect(']'); return l; } }
            if (c == '"') return string();
            if (text.startsWith("true", pos)) { pos += 4; return Boolean.TRUE; }
            if (text.startsWith("false", pos)) { pos += 5; return Boolean.FALSE; }
            if (text.startsWith("null", pos)) { pos += 4; return null; }
            int start = pos; while (pos < text.length() && "+-0123456789.eE".indexOf(text.charAt(pos)) >= 0) pos++;
            if (start == pos) throw error("unexpected character");
            String n = text.substring(start, pos);
            if (n.indexOf('.') >= 0 || n.indexOf('e') >= 0 || n.indexOf('E') >= 0) return Double.parseDouble(n);
            return Long.parseLong(n);
        }
        private String string() {
            expect('"'); StringBuilder b = new StringBuilder();
            while (true) {
                if (pos >= text.length()) throw error("unterminated string");
                char c = text.charAt(pos++);
                if (c == '"') return b.toString();
                if (c != '\\') { b.append(c); continue; }
                if (pos >= text.length()) throw error("unterminated escape");
                char e = text.charAt(pos++);
                switch (e) { case '"': b.append('"'); break; case '\\': b.append('\\'); break; case '/': b.append('/'); break; case 'b': b.append('\b'); break; case 'f': b.append('\f'); break; case 'n': b.append('\n'); break; case 'r': b.append('\r'); break; case 't': b.append('\t'); break; case 'u': if (pos + 4 > text.length()) throw error("short unicode escape"); b.append((char) Integer.parseInt(text.substring(pos, pos + 4), 16)); pos += 4; break; default: throw error("bad escape"); }
            }
        }
        private boolean peek(char c) { return pos < text.length() && text.charAt(pos) == c; }
        private void expect(char c) { if (!peek(c)) throw error("expected '" + c + "'"); pos++; }
        private void skipSpace() { while (pos < text.length() && Character.isWhitespace(text.charAt(pos))) pos++; }
        private IllegalArgumentException error(String message) { return new IllegalArgumentException("job file: " + message + " at " + pos); }
    }
}
