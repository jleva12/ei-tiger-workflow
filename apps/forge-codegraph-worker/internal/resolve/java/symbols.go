package java

import (
	"context"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"unicode"
	"unicode/utf8"

	bc "ei-aitiger-codegraph/pkg/buildcontext"
	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

const symbolBatch = 1000

// Symbol IDs are run-local and deterministic: the same inputs the previous
// implementation hashed, under the graph ID scheme.
func symbolID(file ir.FileID, decl ir.DeclarationID) string {
	return graph.ID("sym", "java-symbol", string(file), string(decl))
}

func intrinsic(n string) semantic.Symbol {
	return semantic.Symbol{ID: graph.ID("sym", "java-intrinsic", n), Name: n, Intrinsic: &semantic.IntrinsicSymbol{Language: "java", Name: n, DefinitionDigest: graph.Digest([]string{Version, "Java primitive/void type", n})}}
}

// symbolWriter batches PutSymbols. Source symbols of attributed files are
// written exactly once by their own file; every other symbol is deduplicated
// by ID because many lookups reach the same external or constructed entity.
type symbolWriter struct {
	ctx     context.Context
	w       semantic.Workspace
	batch   []semantic.Symbol
	seen    map[string]struct{}
	written uint64
}

func newSymbolWriter(ctx context.Context, w semantic.Workspace) *symbolWriter {
	return &symbolWriter{ctx: ctx, w: w, seen: map[string]struct{}{}}
}

func (s *symbolWriter) add(sym semantic.Symbol) error {
	s.batch = append(s.batch, sym)
	if len(s.batch) >= symbolBatch {
		return s.flush()
	}
	return nil
}

func (s *symbolWriter) addUnique(sym semantic.Symbol) (bool, error) {
	if _, ok := s.seen[sym.ID]; ok {
		return false, nil
	}
	s.seen[sym.ID] = struct{}{}
	return true, s.add(sym)
}

func (s *symbolWriter) flush() error {
	if len(s.batch) == 0 {
		return nil
	}
	if err := s.w.PutSymbols(s.ctx, s.batch); err != nil {
		return err
	}
	s.written += uint64(len(s.batch))
	s.batch = s.batch[:0]
	return nil
}

// Key text rules match the previous catalog validation: non-empty, trimmed,
// valid UTF-8, no control characters, bounded length.
func validText(s string, limit int) bool {
	if s == "" || len(s) > limit || !utf8.ValidString(s) || strings.TrimSpace(s) != s {
		return false
	}
	for _, r := range s {
		if unicode.IsControl(r) {
			return false
		}
	}
	return true
}

func validKey(k *semantic.DeclarationKey) bool {
	return k != nil && validText(k.OwnerKey, 4096) && validText(string(k.Kind), 512) && validText(k.Name, 512) && validText(k.CanonicalSignature, 4096)
}

// compilerKey is the canonical declaration key of a source declaration whose
// attributed signature is valid. Local and anonymous types, parameters,
// locals and type parameters have no portable key.
func compilerKey(d *declSummary, e event) *semantic.DeclarationKey {
	switch {
	case d.isType():
		if d.Form == ir.TypeLocal || d.Form == ir.TypeAnonymous {
			return nil
		}
		if d.Form == "" && (e.Nesting == "LOCAL" || e.Nesting == "ANONYMOUS") {
			return nil
		}
	case d.Kind == ir.DeclarationMethod, d.Kind == ir.DeclarationConstructor, d.Kind == ir.DeclarationFunction:
	case d.Kind == ir.DeclarationField, d.Kind == ir.DeclarationEnumConstant, d.Kind == ir.DeclarationRecordComponent:
	default:
		return nil
	}
	signature := e.Signature
	if d.Kind == ir.DeclarationConstructor {
		signature = strings.Replace(signature, "<init>", d.Name, 1)
	}
	owner := e.Owner
	if owner == "" {
		owner = "<default-package>"
	}
	key := &semantic.DeclarationKey{OwnerKey: owner, Kind: d.Kind, Name: d.Name, CanonicalSignature: signature}
	if !validKey(key) {
		return nil
	}
	return key
}

// keyIndex maps compiler signatures to source declarations for the source
// sets whose compiled output is on some attributed classpath. Reactor
// bytecode targets are joined back to source only inside their own set.
type keyIndex struct {
	s       *run
	entries map[keyTuple][]keyEntry
	types   map[typeTuple][]keyEntry // type declarations by qualified name
	loaded  map[bc.SourceSetID]bool
}

type typeTuple struct {
	set       bc.SourceSetID
	signature string
}

type keyTuple struct {
	set       bc.SourceSetID
	owner     string
	kind      string // javac element kind
	name      string
	signature string
}

type keyEntry struct {
	symbol string
	path   string
	decl   ir.DeclarationID
}

func newKeyIndex(s *run) *keyIndex {
	return &keyIndex{s: s, entries: map[keyTuple][]keyEntry{}, types: map[typeTuple][]keyEntry{}, loaded: map[bc.SourceSetID]bool{}}
}

// indexes reports whether a set's declarations must be indexed: only sets
// that other attributed sets see as compiled output.
func (k *keyIndex) indexes(set bc.SourceSetID) bool {
	for _, out := range k.s.outputs {
		if out.ID == set {
			return true
		}
	}
	return false
}

func (k *keyIndex) add(set bc.SourceSetID, e event, v *fileView, d *declSummary) {
	if compilerKey(d, e) == nil {
		return
	}
	k.loaded[set] = true
	entry := keyEntry{symbol: symbolID(v.fileID(), d.ID), path: v.input.Source.Path, decl: d.ID}
	t := keyTuple{set: set, owner: e.Owner, kind: e.ElementKind, name: e.Name, signature: e.Signature}
	k.entries[t] = append(k.entries[t], entry)
	if d.isType() {
		k.types[typeTuple{set, e.Signature}] = append(k.types[typeTuple{set, e.Signature}], entry)
	}
}

// lookupType finds the source type declarations of a set with this
// qualified name (the compiler signature of a type element).
func (k *keyIndex) lookupType(set bc.SourceSetID, signature string) ([]keyEntry, error) {
	if _, attributed := k.s.attributed[set]; !attributed && !k.loaded[set] {
		if err := k.loadPrevious(set); err != nil {
			return nil, err
		}
	}
	return k.types[typeTuple{set, signature}], nil
}

// lookup finds the unique source declaration behind a bytecode member of a
// set. A set that was not attributed in this run is indexed lazily from the
// previous generation's identity keys.
func (k *keyIndex) lookup(set bc.SourceSetID, owner, kind, name, signature string) ([]keyEntry, error) {
	if _, attributed := k.s.attributed[set]; !attributed && !k.loaded[set] {
		if err := k.loadPrevious(set); err != nil {
			return nil, err
		}
	}
	return k.entries[keyTuple{set: set, owner: owner, kind: kind, name: name, signature: signature}], nil
}

func (k *keyIndex) loadPrevious(set bc.SourceSetID) error {
	k.loaded[set] = true
	files, err := k.s.w.Files(k.s.ctx)
	if err != nil {
		return err
	}
	for _, in := range files {
		if bc.SourceSetID(in.Source.SourceSetID) != set {
			continue
		}
		ids, err := k.s.w.PreviousIdentities(k.s.ctx, in.Lineage)
		if errNotFound(err) {
			continue
		}
		if err != nil {
			return err
		}
		for _, d := range ids.Declarations {
			if d.Key == nil {
				continue
			}
			owner := d.Key.OwnerKey
			if owner == "<default-package>" {
				owner = ""
			}
			kind := strings.ToUpper(string(d.Key.Kind))
			name, signature := d.Key.Name, d.Key.CanonicalSignature
			if d.Key.Kind == ir.DeclarationConstructor {
				signature = strings.Replace(signature, name+"(", "<init>(", 1)
				name = "<init>"
			}
			entry := keyEntry{symbol: symbolID(in.Source.FileID, d.DeclarationID), path: in.Source.Path, decl: d.DeclarationID}
			t := keyTuple{set: set, owner: owner, kind: kind, name: name, signature: signature}
			k.entries[t] = append(k.entries[t], entry)
			switch d.Key.Kind {
			case ir.DeclarationClass, ir.DeclarationInterface, ir.DeclarationEnum, ir.DeclarationRecord, ir.DeclarationAnnotationType:
				k.types[typeTuple{set, signature}] = append(k.types[typeTuple{set, signature}], entry)
			}
		}
	}
	return nil
}

// origin is the pinned build input behind a class-file URI.
type origin struct {
	artifactID  string
	fingerprint string
}

// artifactOrigin maps a javac class-file URI to the JDK or JAR input that
// produced it. Every external symbol carries that input's fingerprint.
func (s *run) artifactOrigin(uri string) (origin, error) {
	if o, ok := s.origins[uri]; ok {
		return o, nil
	}
	var o origin
	if strings.HasPrefix(uri, "jrt:") || strings.Contains(uri, "/lib/ct.sym!") {
		for _, jdk := range s.build.Inventory.JDKs {
			input := s.inputs[jdk.HomeInputID]
			path, err := s.inputPath(input)
			if err == nil && path == s.r.javaHome {
				o = origin{artifactID: string(jdk.ID), fingerprint: "sha256:" + input.SHA256}
				break
			}
		}
		if o.artifactID == "" {
			// No build pinned the JDK (a set analysed without its build):
			// the worker's JDK compiled it, under its own identity.
			jdk, err := s.serviceJDK()
			if err != nil {
				return o, err
			}
			o = jdk
		}
	} else {
		raw := strings.TrimPrefix(uri, "jar:")
		raw, _, _ = strings.Cut(raw, "!")
		parsed, err := url.Parse(raw)
		if err != nil {
			return o, fmt.Errorf("%w: %s", errNoOrigin, uri)
		}
		path, err := filepath.EvalSymlinks(filepath.FromSlash(parsed.Path))
		if err != nil {
			return o, fmt.Errorf("%w: %s", errNoOrigin, uri)
		}
		input, ok := s.external[path]
		if !ok {
			for p, in := range s.external {
				if in.Kind == bc.InputClasses && strings.HasPrefix(path, p+string(os.PathSeparator)) {
					input, ok = in, true
					break
				}
			}
		}
		if ok {
			for _, a := range s.build.Inventory.Artifacts {
				if a.BinaryInputID == input.ID {
					o = origin{artifactID: string(a.ID), fingerprint: "sha256:" + input.SHA256}
					break
				}
			}
		}
	}
	if o.artifactID == "" || o.fingerprint == "sha256:" {
		return o, fmt.Errorf("%w: %s", errNoOrigin, uri)
	}
	s.origins[uri] = o
	return o, nil
}

// serviceJDK is the identity of the worker's own JDK, for sets whose build
// pinned none: its tree fingerprint, computed once per process.
func (s *run) serviceJDK() (origin, error) {
	digest, err := s.r.fingerprints.fingerprint(s.ctx, s.r.javaHome, ".", bc.InputJDK, s.limits)
	if err != nil {
		return origin{}, err
	}
	return origin{artifactID: fmt.Sprintf("jdk%d", s.r.jdkMajor), fingerprint: "sha256:" + digest}, nil
}

// externalSymbol is the symbol of a JDK or JAR entity. nil means it has no
// supported origin: a class file from no pinned input (a JAR a manifest
// Class-Path added), or an entity without a portable key (a class in a
// JAR's unnamed package); its lookups are unsupported, not the run.
func (s *run) externalSymbol(e event) (*semantic.Symbol, error) {
	o, err := s.artifactOrigin(e.ArtifactURI)
	if errors.Is(err, errNoOrigin) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	kind := ir.DeclarationKind(strings.ToLower(e.ElementKind))
	if e.ElementKind == "ANNOTATION_TYPE" {
		kind = ir.DeclarationAnnotationType
	}
	key := &semantic.DeclarationKey{OwnerKey: e.Owner, Kind: kind, Name: e.Name, CanonicalSignature: e.Signature}
	if !validKey(key) {
		return nil, nil
	}
	sym := semantic.Symbol{ID: graph.ID("sym", "compiler-external", o.artifactID, o.fingerprint, e.Owner, e.ElementKind, e.Name, e.Signature), Name: e.Name, Key: key, External: &semantic.ExternalSymbol{ArtifactID: o.artifactID, ArtifactFingerprint: o.fingerprint}}
	_, err = s.symbols.addUnique(sym)
	return &sym, err
}

// errNoOrigin reports a class file from no build input of the run.
var errNoOrigin = errors.New("compiler external symbol has no fingerprinted build input")

// sourceOutputSet identifies the source set whose pinned class directory a
// reactor class-file URI comes from.
func (s *run) sourceOutputSet(uri string) (bc.SourceSet, bool) {
	parsed, err := url.Parse(uri)
	if err != nil || parsed.Scheme != "file" {
		return bc.SourceSet{}, false
	}
	path := filepath.Clean(filepath.FromSlash(parsed.Path))
	for root, set := range s.outputs {
		if strings.HasPrefix(path, root+string(filepath.Separator)) {
			return set, true
		}
	}
	return bc.SourceSet{}, false
}

func (s *run) constructedSymbol(kind ir.TypeKind, canonical string, ids []string) (semantic.Symbol, error) {
	constructed := &semantic.ConstructedSymbol{Language: "java", Kind: string(kind), CanonicalSignature: canonical, ComponentSymbolIDs: ids, DefinitionDigest: graph.Digest([]string{Version, "attributed type algebra", string(kind)})}
	sym := semantic.Symbol{ID: graph.ID("sym", "java-constructed-type", string(kind), canonical, strings.Join(ids, "\x00")), Name: canonical, Constructed: constructed}
	_, err := s.symbols.addUnique(sym)
	return sym, err
}

// arrayMember is a language-defined array operation (length, clone, an
// array constructor reference) with no declaration anywhere.
func (s *run) arrayMember(e event, kind ir.DeclarationKind, rule string) (semantic.Symbol, error) {
	sym := semantic.Symbol{ID: graph.ID("sym", "java-intrinsic-member", "array", e.Name), Name: e.Name, Key: &semantic.DeclarationKey{OwnerKey: e.Owner, Kind: kind, Name: e.Name, CanonicalSignature: e.Signature}, Intrinsic: &semantic.IntrinsicSymbol{Language: "java", Name: e.Name, DefinitionDigest: graph.Digest([]string{Version, rule})}}
	_, err := s.symbols.addUnique(sym)
	return sym, err
}

// Javac locates an enum constant's anonymous class over the entire constant;
// the parser records its body as a class owned by that exact enum declaration.
func compilerOwnerMatches(v *fileView, d *declSummary, e event) bool {
	if !d.isType() || e.OwnerSourceStart < 0 || e.OwnerSourceEnd < 0 {
		return false
	}
	span := d.Span
	if d.Form == ir.TypeAnonymous {
		if parent, ok := v.decl(d.OwnerID); ok && parent.Kind == ir.DeclarationEnumConstant {
			span = parent.Span
		}
	}
	return uint64(e.OwnerSourceStart) == span.Start.ByteOffset && uint64(e.OwnerSourceEnd) == span.End.ByteOffset
}

// derivedSymbol represents a compiler-established member without written
// syntax (implicit constructor, enum values/valueOf, record accessors) tied
// to its exact attributed source owner.
func (s *run) derivedSymbol(v *fileView, e event) (*semantic.Symbol, error) {
	var owner *declSummary
	for i := range v.decls {
		d := &v.decls[i]
		if e.OwnerSourcePath == v.bridgePath && compilerOwnerMatches(v, d, e) {
			owner = d
			break
		}
		if d.Qualified != "" && d.Qualified == e.Owner {
			owner = d
			break
		}
	}
	if owner == nil {
		return nil, nil
	}
	rule := ""
	switch {
	case e.ElementKind == "CONSTRUCTOR":
		rule = "implicit_constructor"
	case owner.Kind == ir.DeclarationEnum:
		rule = "enum_builtin_member"
	case owner.Kind == ir.DeclarationRecord:
		rule = "record_builtin_member"
	default:
		return nil, nil
	}
	key := &semantic.DeclarationKey{OwnerKey: e.Owner, Kind: ir.DeclarationKind(strings.ToLower(e.ElementKind)), Name: e.Name, CanonicalSignature: e.Signature}
	if !validKey(key) {
		return nil, nil
	}
	// The owner is the contributor: its symbol must exist, which a set not
	// attributed in this run does not write itself.
	source, err := s.sourceTarget(v, owner)
	if err != nil {
		return nil, err
	}
	ownerID := source.ID
	derived := &semantic.DerivedSymbol{Language: "java", Rule: rule, SourceSymbolIDs: []string{ownerID}, DefinitionDigest: graph.Digest([]string{Version, rule, "javac source-owner-derived member"})}
	sym := semantic.Symbol{ID: graph.ID("sym", "java-derived-member", rule, ownerID, e.Name, e.Signature), Name: e.Name, Key: key, OwnerSymbolID: ownerID, Derived: derived}
	if _, err := s.symbols.addUnique(sym); err != nil {
		return nil, err
	}
	return &sym, nil
}

// lombokRule is the derivation rule of what Lombok generates.
const lombokRule = "lombok_generated"

// generatedSymbol maps a target the bridge marked as Lombok-generated in
// this context's sources to its derived symbol. nil means its source owner
// could not be found.
func (s *run) generatedSymbol(e event) (*semantic.Symbol, error) {
	if e.Generated != "lombok" || e.SourceOwner == "" || e.SourceOwnerPath == "" {
		return nil, nil
	}
	v, err := s.views.byBridgePath(e.SourceOwnerPath)
	if err != nil {
		return nil, err
	}
	for i := range v.decls {
		d := &v.decls[i]
		if !d.isType() {
			continue
		}
		if (e.SourceOwnerStart >= 0 && uint64(e.SourceOwnerStart) == d.Span.Start.ByteOffset && uint64(e.SourceOwnerEnd) == d.Span.End.ByteOffset) || (d.Qualified != "" && d.Qualified == e.SourceOwner) {
			return s.lombokSymbol(v, d, e.SourceOwner, e)
		}
	}
	return nil, nil
}

// lombokSymbol is the derived symbol of a member or type Lombok generated
// inside the source type owner (qualified name ownerName). Types Lombok
// generated between them, such as a builder, are derived symbols too and
// own the member. The source type is every one's contributor: bytecode of
// another module says which type a member belongs to, not which field it
// came from, so both ways to the member reach the same symbol.
func (s *run) lombokSymbol(v *fileView, owner *declSummary, ownerName string, e event) (*semantic.Symbol, error) {
	kind, ok := elementKind(e)
	if !ok || (kind != ir.DeclarationClass && kind != ir.DeclarationMethod && kind != ir.DeclarationConstructor && kind != ir.DeclarationField) {
		return nil, nil
	}
	source, err := s.sourceTarget(v, owner)
	if err != nil {
		return nil, err
	}
	contributor := source.ID
	derive := func(ownerSymbol string, key *semantic.DeclarationKey) (*semantic.Symbol, error) {
		if !validKey(key) {
			return nil, nil
		}
		derived := &semantic.DerivedSymbol{Language: "java", Rule: lombokRule, SourceSymbolIDs: []string{contributor}, DefinitionDigest: graph.Digest([]string{Version, lombokRule, "Lombok-generated member of its nearest source type"})}
		sym := semantic.Symbol{ID: graph.ID("sym", "java-derived-member", lombokRule, contributor, key.OwnerKey, string(key.Kind), key.Name, key.CanonicalSignature), Name: key.Name, Key: key, OwnerSymbolID: ownerSymbol, Derived: derived}
		if _, err := s.symbols.addUnique(sym); err != nil {
			return nil, err
		}
		return &sym, nil
	}
	// The generated types from the source owner down to the target's owner.
	parent, parentName := contributor, ownerName
	if e.Owner != ownerName {
		rest, ok := strings.CutPrefix(e.Owner, ownerName+".")
		if !ok {
			return nil, nil
		}
		for _, name := range strings.Split(rest, ".") {
			sym, err := derive(parent, &semantic.DeclarationKey{OwnerKey: parentName, Kind: ir.DeclarationClass, Name: name, CanonicalSignature: parentName + "." + name})
			if err != nil || sym == nil {
				return nil, err
			}
			parent, parentName = sym.ID, parentName+"."+name
		}
	}
	return derive(parent, &semantic.DeclarationKey{OwnerKey: e.Owner, Kind: kind, Name: e.Name, CanonicalSignature: e.Signature})
}

// usesLombok reports whether Lombok is on a source set's classpath, so
// members its bytecode has and its sources lack were generated by Lombok.
func (s *run) usesLombok(id bc.SourceSetID) bool {
	for _, entry := range s.sets[id].Classpath {
		if a, ok := s.artifacts[bc.ArtifactID(entry.RefID)]; ok && entry.Kind == bc.EntryArtifact && a.Coordinates.Group == "org.projectlombok" && a.Coordinates.Name == "lombok" {
			return true
		}
	}
	return false
}

// Javac's implicit compact-constructor parameters point at their record header
// components but have no source end position. Verify the exact compiler-owned
// constructor and component start; retain both real contributors as provenance.
// Explicit constructor parameters continue through ordinary source binding.
func (s *run) compactRecordParameter(v *fileView, fe *fileEvents, e event) (*semantic.Symbol, error) {
	if v.file == nil || fe == nil || fe.path != v.bridgePath || e.TargetPath != v.bridgePath {
		return nil, nil
	}
	for i := range v.decls {
		constructor := &v.decls[i]
		if constructor.Kind != ir.DeclarationConstructor || constructor.Callable == nil || constructor.Callable.ConstructorForm != ir.ConstructorCompact {
			continue
		}
		owner, ok := v.decl(constructor.OwnerID)
		if !ok || owner.Kind != ir.DeclarationRecord || owner.Type == nil {
			continue
		}
		attributed, found := fe.declarationFor(constructor)
		if !found || attributed.SignatureValid != "true" || attributed.Owner+"#"+attributed.Signature != e.Owner {
			continue
		}
		for _, componentID := range owner.Type.RecordComponentIDs {
			component, ok := v.decl(componentID)
			if !ok || component.Kind != ir.DeclarationRecordComponent || component.Name != e.Name || int64(component.Span.Start.ByteOffset) != e.TargetStart {
				continue
			}
			constructorSymbol := symbolID(v.fileID(), constructor.ID)
			componentSymbol := symbolID(v.fileID(), component.ID)
			rule := "compact_record_constructor_parameter"
			key := &semantic.DeclarationKey{OwnerKey: e.Owner, Kind: ir.DeclarationParameter, Name: e.Name, CanonicalSignature: e.Signature}
			if !validKey(key) {
				return nil, nil
			}
			sym := semantic.Symbol{ID: graph.ID("sym", "java-derived-parameter", rule, constructorSymbol, componentSymbol, e.Signature), Name: e.Name, Key: key, OwnerSymbolID: constructorSymbol,
				Derived: &semantic.DerivedSymbol{Language: "java", Rule: rule, SourceSymbolIDs: []string{constructorSymbol, componentSymbol}, DefinitionDigest: graph.Digest([]string{Version, rule})}}
			if _, err := s.symbols.addUnique(sym); err != nil {
				return nil, err
			}
			return &sym, nil
		}
	}
	return nil, nil
}
