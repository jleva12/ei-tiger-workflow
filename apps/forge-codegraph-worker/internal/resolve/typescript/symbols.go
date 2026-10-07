package typescript

import (
	"context"
	"strings"
	"unicode"
	"unicode/utf8"

	"ei-aitiger-codegraph/pkg/graph"
	"ei-aitiger-codegraph/pkg/ir"
	"ei-aitiger-codegraph/pkg/semantic"
)

const symbolBatch = 1000

func symbolID(file ir.FileID, id ir.DeclarationID) string {
	return graph.ID("sym", "typescript-symbol", string(file), string(id))
}

// intrinsic names a language- or runtime-defined entity with no source
// declaration: predefined types and well-known globals.
func intrinsic(name string) semantic.Symbol {
	return semantic.Symbol{ID: graph.ID("sym", "typescript-intrinsic", name), Name: name, Intrinsic: &semantic.IntrinsicSymbol{Language: Language, Name: name, DefinitionDigest: graph.Digest([]string{Version, "TypeScript intrinsic", name})}}
}

// predefinedTypes are the type keywords and the global types every program
// sees without a declaration.
var predefinedTypes = map[string]bool{"string": true, "number": true, "boolean": true, "any": true, "unknown": true, "never": true, "void": true, "object": true, "symbol": true, "bigint": true, "null": true, "undefined": true,
	"Array": true, "ReadonlyArray": true, "Promise": true, "Map": true, "Set": true, "WeakMap": true, "WeakSet": true, "Record": true, "Partial": true, "Required": true, "Readonly": true, "Pick": true, "Omit": true, "Exclude": true, "Extract": true, "NonNullable": true, "ReturnType": true, "Parameters": true, "InstanceType": true, "Awaited": true,
	"Date": true, "Error": true, "RegExp": true, "Function": true, "Object": true, "String": true, "Number": true, "Boolean": true, "Symbol": true, "Iterable": true, "Iterator": true, "IterableIterator": true, "Generator": true, "AsyncIterable": true, "ArrayBuffer": true, "Uint8Array": true,
	"Element": true, "HTMLElement": true, "Event": true, "Window": true, "Document": true, "Node": true, "Response": true, "Request": true, "Headers": true, "URL": true, "URLSearchParams": true, "FormData": true, "AbortSignal": true, "AbortController": true}

// transparentTypes are utility types that denote their first argument's
// members for binding purposes.
var transparentTypes = map[string]bool{"Readonly": true, "Partial": true, "Required": true, "NonNullable": true, "Awaited": true, "Omit": true, "Pick": true}

// frameworkGlobals are names a test framework or a UMD dependency injects
// as globals; they are dependencies, never intrinsics.
var frameworkGlobals = map[string]string{
	"describe": "the test framework", "it": "the test framework", "test": "the test framework", "expect": "the test framework", "vi": "vitest", "jest": "jest",
	"beforeEach": "the test framework", "afterEach": "the test framework", "beforeAll": "the test framework", "afterAll": "the test framework", "suite": "the test framework", "bench": "vitest",
	"cy": "cypress", "Cypress": "cypress", "React": "react (UMD global)", "JSX": "react (JSX namespace)", "$": "jquery", "jQuery": "jquery", "chrome": "the browser extension API",
}

// platformNames are the DOM, Web and Node platform values and types that a
// program names without declaring or importing them.
var platformNames = []string{
	"self", "global", "arguments", "requestAnimationFrame", "cancelAnimationFrame", "requestIdleCallback", "cancelIdleCallback", "performance", "crypto", "atob", "btoa", "eval", "escape", "unescape",
	"setImmediate", "clearImmediate", "reportError", "dispatchEvent", "addEventListener", "removeEventListener", "getComputedStyle", "matchMedia", "scrollTo", "scrollBy", "open", "close", "postMessage", "prompt", "confirm", "print", "focus", "blur",
	"devicePixelRatio", "innerWidth", "innerHeight", "outerWidth", "outerHeight", "scrollX", "scrollY", "pageXOffset", "pageYOffset", "origin", "parent", "top", "frames", "screen", "visualViewport", "speechSynthesis", "indexedDB", "caches", "customElements", "CSS",
	"Blob", "File", "FileReader", "FileList", "Image", "Audio", "Option", "ImageData", "ImageBitmap", "Path2D", "OffscreenCanvas", "Worker", "SharedWorker", "ServiceWorker", "MessageChannel", "MessagePort", "BroadcastChannel", "WebSocket", "XMLHttpRequest", "EventSource", "EventTarget",
	"Event", "CustomEvent", "KeyboardEvent", "MouseEvent", "PointerEvent", "TouchEvent", "WheelEvent", "DragEvent", "ClipboardEvent", "FocusEvent", "InputEvent", "UIEvent", "ErrorEvent", "MessageEvent", "PopStateEvent", "HashChangeEvent", "StorageEvent", "ProgressEvent", "CloseEvent", "AnimationEvent", "TransitionEvent", "CompositionEvent", "SubmitEvent", "BeforeUnloadEvent", "PromiseRejectionEvent", "GamepadEvent",
	"Node", "Element", "HTMLElement", "HTMLDivElement", "HTMLSpanElement", "HTMLInputElement", "HTMLTextAreaElement", "HTMLButtonElement", "HTMLCanvasElement", "HTMLImageElement", "HTMLVideoElement", "HTMLAudioElement", "HTMLMediaElement", "HTMLAnchorElement", "HTMLFormElement", "HTMLSelectElement", "HTMLOptionElement", "HTMLLabelElement", "HTMLIFrameElement", "HTMLTableElement", "HTMLTableRowElement", "HTMLTableCellElement", "HTMLTableSectionElement", "HTMLUListElement", "HTMLOListElement", "HTMLLIElement", "HTMLParagraphElement", "HTMLHeadingElement", "HTMLScriptElement", "HTMLStyleElement", "HTMLLinkElement", "HTMLTemplateElement", "HTMLDialogElement", "HTMLSlotElement", "HTMLBodyElement", "HTMLHtmlElement", "HTMLHeadElement", "HTMLMetaElement", "HTMLBRElement", "HTMLHRElement", "HTMLPreElement", "HTMLProgressElement", "HTMLDetailsElement", "HTMLFieldSetElement", "HTMLDataListElement", "HTMLPictureElement", "HTMLSourceElement", "HTMLObjectElement", "HTMLEmbedElement", "HTMLCollection", "NodeList", "NamedNodeMap", "Attr", "CharacterData", "ShadowRoot",
	"SVGElement", "SVGSVGElement", "SVGGraphicsElement", "SVGPathElement", "SVGGElement", "Document", "DocumentFragment", "Text", "Comment", "Range", "Selection", "DOMParser", "XMLSerializer", "DOMRect", "DOMRectReadOnly", "DOMPoint", "DOMMatrix", "DOMException", "TreeWalker", "NodeIterator", "NodeFilter", "MutationRecord",
	"CanvasRenderingContext2D", "CanvasGradient", "CanvasPattern", "OffscreenCanvasRenderingContext2D", "WebGLRenderingContext", "WebGL2RenderingContext", "AudioContext", "AudioBuffer", "AudioNode", "MediaStream", "MediaRecorder", "ResizeObserver", "ResizeObserverEntry", "IntersectionObserver", "IntersectionObserverEntry", "MutationObserver", "PerformanceObserver", "Notification", "Cache", "IDBDatabase", "IDBRequest", "Storage", "Location", "History", "Navigator", "Window", "Screen", "Performance", "Crypto", "SubtleCrypto", "Clipboard", "ClipboardItem", "DataTransfer", "DataTransferItem", "Touch", "TouchList", "Gamepad", "Geolocation", "CSSStyleDeclaration", "CSSStyleSheet", "StyleSheet", "MediaQueryList", "Animation", "KeyframeEffect",
	"TextEncoder", "TextDecoder", "ReadableStream", "WritableStream", "TransformStream", "CompressionStream", "DecompressionStream", "AbortSignal", "AbortController",
	"Uint8Array", "Uint16Array", "Uint32Array", "Int8Array", "Int16Array", "Int32Array", "Float32Array", "Float64Array", "Uint8ClampedArray", "BigInt64Array", "BigUint64Array", "ArrayBuffer", "SharedArrayBuffer", "DataView", "BigInt", "WeakRef", "FinalizationRegistry", "Atomics", "WebAssembly", "Iterator", "AggregateError", "SyntaxError", "ReferenceError", "EvalError", "URIError", "Deno", "Bun",
}

// platformTypes are TypeScript library types with no value counterpart.
var platformTypes = []string{
	"ConstructorParameters", "ThisParameterType", "OmitThisParameter", "ThisType", "Uppercase", "Lowercase", "Capitalize", "Uncapitalize", "NoInfer", "PropertyKey", "PropertyDescriptor", "PropertyDescriptorMap", "TemplateStringsArray", "ArrayLike", "PromiseLike", "AsyncIterator", "AsyncIterableIterator", "AsyncGenerator", "ReadonlyMap", "ReadonlySet", "ConcatArray", "ArrayBufferLike", "ArrayBufferView", "BufferSource", "ImageBitmapSource", "CanvasImageSource", "TexImageSource", "RenderingContext", "ScrollBehavior", "ScrollToOptions", "ScrollIntoViewOptions", "IdleDeadline", "FrameRequestCallback", "TimerHandler", "GlobalEventHandlers", "WindowEventMap", "DocumentEventMap", "HTMLElementEventMap", "HTMLElementTagNameMap", "SVGElementTagNameMap", "EventListener", "EventListenerObject", "AddEventListenerOptions", "EventInit", "MouseEventInit", "KeyboardEventInit", "PointerEventInit", "RequestInit", "ResponseInit", "HeadersInit", "BodyInit", "BlobPart", "BlobPropertyBag", "FilePropertyBag", "NodeJS", "Intl", "ErrorOptions", "Disposable", "AsyncDisposable", "IteratorResult", "IteratorReturnResult", "IteratorYieldResult", "CallableFunction", "NewableFunction", "IArguments", "RegExpMatchArray", "RegExpExecArray",
}

func init() {
	for _, name := range platformNames {
		globals[name] = true
		predefinedTypes[name] = true
	}
	for _, name := range platformTypes {
		predefinedTypes[name] = true
	}
}

// globals are runtime values every module can name without importing them.
var globals = map[string]bool{"console": true, "window": true, "document": true, "globalThis": true, "process": true, "require": true, "module": true, "exports": true, "Promise": true, "Array": true, "Object": true, "JSON": true, "Math": true, "Number": true, "String": true, "Boolean": true, "Date": true, "Map": true, "Set": true, "WeakMap": true, "WeakSet": true, "Error": true, "TypeError": true, "RangeError": true, "Symbol": true, "RegExp": true, "Reflect": true, "Proxy": true, "Intl": true,
	"setTimeout": true, "setInterval": true, "clearTimeout": true, "clearInterval": true, "queueMicrotask": true, "structuredClone": true, "fetch": true, "parseInt": true, "parseFloat": true, "isNaN": true, "isFinite": true, "encodeURIComponent": true, "decodeURIComponent": true, "encodeURI": true, "decodeURI": true, "NaN": true, "Infinity": true, "undefined": true, "alert": true, "localStorage": true, "sessionStorage": true, "navigator": true, "location": true, "history": true, "URL": true, "URLSearchParams": true, "FormData": true, "Headers": true, "Request": true, "Response": true, "AbortController": true, "Buffer": true, "__dirname": true, "__filename": true}

// symbolWriter batches PutSymbols and deduplicates intrinsics.
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

func (s *symbolWriter) addUnique(sym semantic.Symbol) error {
	if _, ok := s.seen[sym.ID]; ok {
		return nil
	}
	s.seen[sym.ID] = struct{}{}
	return s.add(sym)
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

// keyOwnerKinds are the declarations whose members carry portable keys.
func keyOwnerKind(kind ir.DeclarationKind) bool {
	switch kind {
	case ir.DeclarationClass, ir.DeclarationInterface, ir.DeclarationEnum, ir.DeclarationNamespace, ir.DeclarationTypeAlias, ir.DeclarationVariable, ir.DeclarationField:
		return true
	}
	return false
}

// key is the syntax-derived canonical key of a named module-level or member
// declaration: the module path, the owner chain and the name, with parameter
// names for callables. Locals, parameters, type parameters and anonymous
// types have no portable key. It is stable across commits that keep the
// declaration in place, which is what the matcher needs for continuity.
func (s *run) key(m *module, d *decl) *semantic.DeclarationKey {
	if d.Key != nil {
		return d.Key
	}
	switch d.Kind {
	case ir.DeclarationClass, ir.DeclarationInterface, ir.DeclarationEnum, ir.DeclarationTypeAlias, ir.DeclarationNamespace, ir.DeclarationFunction, ir.DeclarationMethod, ir.DeclarationConstructor, ir.DeclarationField, ir.DeclarationVariable, ir.DeclarationEnumConstant:
	default:
		return nil
	}
	if d.Name == "" {
		return nil
	}
	var chain []string
	for cur, depth := d, 0; cur.OwnerID != "" && depth < 64; depth++ {
		owner, ok := m.decl(cur.OwnerID)
		if !ok || !keyOwnerKind(owner.Kind) || owner.Name == "" {
			return nil
		}
		chain = append([]string{owner.Name}, chain...)
		cur = owner
	}
	ownerKey := m.path
	if len(chain) > 0 {
		ownerKey = m.path + "#" + strings.Join(chain, ".")
	}
	signature := m.path + "#" + strings.Join(append(chain, d.Name), ".")
	if d.Callable != nil {
		var params []string
		for _, id := range d.Callable.ParameterIDs {
			if p, ok := m.decl(id); ok {
				params = append(params, p.Name)
			}
		}
		signature += "(" + strings.Join(params, ", ") + ")"
	}
	key := &semantic.DeclarationKey{OwnerKey: ownerKey, Kind: d.Kind, Name: d.Name, CanonicalSignature: signature}
	if !validText(key.OwnerKey, 4096) || !validText(key.Name, 512) || !validText(key.CanonicalSignature, 4096) {
		return nil
	}
	return key
}

func (s *run) sourceSymbol(m *module, d *decl) semantic.Symbol {
	sym := semantic.Symbol{ID: symbolID(m.in.Source.FileID, d.ID), Name: d.Name, Key: s.key(m, d), Source: &semantic.SourceSymbol{FileID: m.in.Source.FileID, DeclarationID: d.ID, Evidence: m.anchor(d.Span)}}
	if d.OwnerID != "" {
		sym.OwnerSymbolID = symbolID(m.in.Source.FileID, d.OwnerID)
	}
	return sym
}

// frameworkPackages names the package, and the export in it, behind each
// framework global that has one; the test globals (describe, it, expect)
// belong to whichever runner the project uses, so they are named as the
// runner-neutral test API.
var frameworkPackages = map[string]struct{ pkg, path string }{
	"describe": {testAPI, "describe"}, "it": {testAPI, "it"}, "test": {testAPI, "test"}, "expect": {testAPI, "expect"},
	"beforeEach": {testAPI, "beforeEach"}, "afterEach": {testAPI, "afterEach"}, "beforeAll": {testAPI, "beforeAll"}, "afterAll": {testAPI, "afterAll"}, "suite": {testAPI, "suite"},
	"vi": {"vitest", "vi"}, "bench": {"vitest", "bench"}, "jest": {"@jest/globals", "jest"},
	"cy": {"cypress", "cy"}, "Cypress": {"cypress", "Cypress"}, "React": {"react", ""}, "JSX": {"react", "JSX"},
	"$": {"jquery", ""}, "jQuery": {"jquery", ""}, "chrome": {"global:chrome-extensions", "chrome"},
}

// testAPI stands for the test runner's globals.
const testAPI = "global:test-api"

// platformNamespaces are platform objects whose methods return something
// other than themselves: a call on one is named by the call, not typed as
// the object.
var platformNamespaces = map[string]bool{"JSON": true, "Math": true, "Reflect": true, "Object": true, "console": true, "process": true, "window": true, "document": true, "navigator": true, "location": true, "history": true, "localStorage": true, "sessionStorage": true, "globalThis": true, "Intl": true, "crypto": true, "performance": true, "Atomics": true, "WebAssembly": true, "Deno": true, "Bun": true}

// nodeBuiltins are the modules Node provides; "fs" and "node:fs" are one.
var nodeBuiltins = map[string]bool{"assert": true, "async_hooks": true, "buffer": true, "child_process": true, "cluster": true, "console": true, "constants": true, "crypto": true, "dgram": true, "diagnostics_channel": true, "dns": true, "domain": true, "events": true, "fs": true, "http": true, "http2": true, "https": true, "inspector": true, "module": true, "net": true, "os": true, "path": true, "perf_hooks": true, "process": true, "punycode": true, "querystring": true, "readline": true, "repl": true, "stream": true, "string_decoder": true, "sys": true, "test": true, "timers": true, "tls": true, "trace_events": true, "tty": true, "url": true, "util": true, "v8": true, "vm": true, "wasi": true, "worker_threads": true, "zlib": true}

// canonicalSpecifier writes a package specifier one way: a Node built-in
// with its node: prefix, anything else as written.
func canonicalSpecifier(spec string) string {
	if strings.HasPrefix(spec, "node:") || strings.HasPrefix(spec, "global:") {
		return spec
	}
	root, _, _ := strings.Cut(spec, "/")
	if nodeBuiltins[root] {
		return "node:" + spec
	}
	return spec
}

// packageSpecifier reports whether a specifier names a package: a Node
// built-in, or an npm name (lower-case, optionally @scope/) with an
// optional subpath. Path aliases (@/x, ~/x, #x) are not packages.
func packageSpecifier(spec string) bool {
	if strings.HasPrefix(spec, "node:") {
		return len(spec) > len("node:")
	}
	name := packageName(spec)
	segments := strings.Split(strings.TrimPrefix(name, "@"), "/")
	if strings.HasPrefix(name, "@") && len(segments) != 2 {
		return false
	}
	for _, segment := range segments {
		if segment == "" || len(segment) > 214 {
			return false
		}
		for i, r := range segment {
			switch {
			case r >= 'a' && r <= 'z', r >= '0' && r <= '9':
			case i > 0 && (r == '-' || r == '.' || r == '_'):
			default:
				return false
			}
		}
	}
	return true
}

// packageName is the package a specifier names: its first segment, or its
// first two for a scoped package (@tanstack/react-query/devtools).
func packageName(spec string) string {
	parts := strings.SplitN(spec, "/", 3)
	if strings.HasPrefix(spec, "@") && len(parts) >= 2 {
		return parts[0] + "/" + parts[1]
	}
	return parts[0]
}

// externalArtifact is the artifact an external symbol belongs to: the npm
// package, the Node built-in module, or a platform or framework API.
func externalArtifact(pkg string) string {
	switch {
	case strings.HasPrefix(pkg, "node:"):
		root, _, _ := strings.Cut(strings.TrimPrefix(pkg, "node:"), "/")
		return "node:" + root
	case strings.HasPrefix(pkg, "global:"):
		return pkg
	}
	return "npm:" + packageName(pkg)
}

// externalFingerprint pins no version: a checkout has no installed
// packages, so a package's symbols are named, not read, and stay the same
// entities across upgrades.
const externalFingerprint = "unversioned"

// externalSymbol is the symbol of a value a package exports, or of a
// member or call result taken from one, named by its path: react#useState,
// axios#create().get. It carries no declaration: nothing is installed to
// read one from.
func externalSymbol(pkg, path string) semantic.Symbol {
	name, owner := path, ""
	if i := strings.LastIndexAny(path, "."); i >= 0 {
		owner, name = path[:i], path[i+1:]
	}
	if name == "" {
		name = pkg
	}
	key := &semantic.DeclarationKey{OwnerKey: pkg + "#" + owner, Kind: ir.DeclarationVariable, Name: name, CanonicalSignature: pkg + "#" + path}
	artifact := externalArtifact(pkg)
	return semantic.Symbol{ID: graph.ID("sym", "typescript-external", artifact, key.CanonicalSignature), Name: name, Key: key, External: &semantic.ExternalSymbol{ArtifactID: artifact, ArtifactFingerprint: externalFingerprint}}
}
