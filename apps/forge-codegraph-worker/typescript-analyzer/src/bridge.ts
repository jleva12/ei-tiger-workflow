// The TypeScript compiler bridge binds the sites the Go resolver asks about
// with the real type checker. It reads one JSON request on stdin (the
// source files of a context, written under root, and the sites of the files
// that changed) and writes one JSON event per line: hello, a lookup for
// every site the checker binds to a declaration, file_done per file, done.
// A site it cannot bind (an any-typed receiver, a keyword type, a module
// namespace) gets no lookup: the Go resolver keeps its syntax-tier binding.
import fs from 'node:fs';
import path from 'node:path';
import ts from 'typescript';

type SiteKind = 'call' | 'name' | 'member' | 'type' | 'decorator' | 'override';
interface Site { id: string; kind: SiteKind; start: number; end: number }
interface FileRequest { id: string; path: string; sites?: Site[] }
interface Request { protocol: number; context: string; root: string; files: FileRequest[]; packages?: { name: string; dir: string }[] }
// A target is a declaration in the checkout (by file and name span), in a
// package (by package and qualified name), or in the platform library.
interface Owner { start: number; end: number; member: string }
interface Target { module: 'source' | 'package' | 'lib'; name: string; path?: string; start?: number; end?: number; owners?: Owner[]; pkg?: string; qualified?: string }

const request: Request = JSON.parse(fs.readFileSync(0, 'utf8'));
if (request.protocol !== 1) throw new Error('unsupported protocol ' + request.protocol);
const root = request.root.replace(/\/+$/, '');
const libDir = path.join(__dirname, 'lib');
const started = Date.now();

let pending = '';
function emit(event: object) {
  pending += JSON.stringify(event) + '\n';
  if (pending.length > 1 << 16) flush();
}
function flush() {
  if (pending) fs.writeSync(1, pending);
  pending = '';
}

// Byte offsets: the Go side speaks UTF-8 bytes, the compiler UTF-16 units.
const tables = new Map<string, Uint32Array>();
function bytesOf(sf: ts.SourceFile): Uint32Array {
  let table = tables.get(sf.fileName);
  if (table) return table;
  const text = sf.text;
  table = new Uint32Array(text.length + 1);
  let b = 0;
  for (let i = 0; i < text.length; i++) {
    table[i] = b;
    const c = text.charCodeAt(i);
    if (c < 0x80) b += 1;
    else if (c < 0x800) b += 2;
    else if (c >= 0xd800 && c <= 0xdbff && i + 1 < text.length && (text.charCodeAt(i + 1) & 0xfc00) === 0xdc00) {
      table[i + 1] = b;
      b += 4;
      i++;
    } else b += 3;
  }
  table[text.length] = b;
  tables.set(sf.fileName, table);
  return table;
}

const inCheckout = (file: string) => file.startsWith(root + '/') && !file.includes('/node_modules/');

// Files of the checkout are read as written, with a byte order mark kept so
// offsets stay those of the file; the compiler skips it as whitespace.
function readFile(file: string): string | undefined {
  if (!inCheckout(file)) return ts.sys.readFile(file);
  try { return fs.readFileSync(file).toString('utf8'); } catch { return undefined; }
}

// Parsed files are shared by the programs of every project, since the
// declaration files of packages and of the platform dominate parsing.
const sourceFiles = new Map<string, ts.SourceFile>();
function hostFor(options: ts.CompilerOptions): ts.CompilerHost {
  const host = ts.createCompilerHost(options, true);
  const parse = host.getSourceFile;
  host.readFile = readFile;
  host.getDefaultLibLocation = () => libDir;
  host.getDefaultLibFileName = (o) => path.join(libDir, ts.getDefaultLibFileName(o));
  // A workspace package whose entries are its build output, not committed
  // (exports "./audit": "./dist/audit/index.d.ts"), resolves to the sources
  // that output is built from, as project references map outputs to
  // sources.
  const cache = ts.createModuleResolutionCache(root, f => f, options);
  host.resolveModuleNameLiterals = (literals, containingFile, redirected, compilerOptions, containingSourceFile) => literals.map(literal => {
    let mode: ts.ResolutionMode;
    try { mode = ts.getModeForUsageLocation(containingSourceFile, literal, compilerOptions); } catch { mode = undefined; }
    const result = ts.resolveModuleName(literal.text, containingFile, compilerOptions, host, cache, redirected, mode);
    if (result.resolvedModule) return result;
    const source = workspaceSource(literal.text, containingFile);
    if (!source) return result;
    return { ...result, resolvedModule: { resolvedFileName: source, extension: extensionOf(source), isExternalLibraryImport: false } as ts.ResolvedModuleFull };
  });
  host.getSourceFile = (fileName, languageVersionOrOptions, onError, shouldCreate) => {
    const version = typeof languageVersionOrOptions === 'object'
      ? `${languageVersionOrOptions.languageVersion}/${languageVersionOrOptions.impliedNodeFormat ?? ''}`
      : String(languageVersionOrOptions);
    const key = `${fileName}|${version}|${options.moduleDetection ?? ''}`;
    let sf = sourceFiles.get(key);
    if (!sf) {
      sf = parse.call(host, fileName, languageVersionOrOptions, onError, shouldCreate);
      if (sf) sourceFiles.set(key, sf);
    }
    return sf;
  };
  return host;
}

const workspacePackages = new Map((request.packages ?? []).map(p => [p.name, path.resolve(p.dir)]));
const manifests = new Map<string, any>();
function manifestOf(dir: string): any {
  if (!manifests.has(dir)) {
    let manifest: any;
    try { manifest = JSON.parse(fs.readFileSync(path.join(dir, 'package.json'), 'utf8')); } catch { manifest = undefined; }
    manifests.set(dir, manifest);
  }
  return manifests.get(dir);
}
// packageDir is the directory of a workspace package of the checkout: one
// the project discovery found, or one node_modules links into the checkout.
function packageDir(name: string, from: string): string | undefined {
  const known = workspacePackages.get(name);
  if (known) return known;
  for (let dir = path.dirname(from); dir.startsWith(root); dir = path.dirname(dir)) {
    const candidate = path.join(dir, 'node_modules', name);
    if (fs.existsSync(candidate)) {
      try {
        const real = fs.realpathSync(candidate);
        return inCheckout(real) ? real : undefined;
      } catch { return undefined; }
    }
    if (dir === root) break;
  }
  return undefined;
}
const conditions = ['types', 'typings', 'import', 'module', 'default', 'require', 'node', 'browser'];
function collect(value: any, star?: string): string[] {
  if (typeof value === 'string') return [star === undefined ? value : value.replace('*', star)];
  if (Array.isArray(value)) return value.flatMap(v => collect(v, star));
  if (value && typeof value === 'object') {
    const keys = [...conditions.filter(c => c in value), ...Object.keys(value).filter(k => !conditions.includes(k))];
    return keys.flatMap(k => collect(value[k], star));
  }
  return [];
}
// entryTargets are the files a package's manifest names for a subpath, in
// preference order.
function entryTargets(manifest: any, sub: string): string[] {
  const exports = manifest?.exports;
  const key = sub ? './' + sub : '.';
  const out: string[] = [];
  if (typeof exports === 'string' || Array.isArray(exports)) {
    if (!sub) out.push(...collect(exports));
  } else if (exports && typeof exports === 'object') {
    const subpaths = Object.keys(exports).some(k => k.startsWith('.'));
    if (!subpaths) {
      if (!sub) out.push(...collect(exports));
    } else if (key in exports) {
      out.push(...collect(exports[key]));
    } else {
      for (const pattern of Object.keys(exports)) {
        const [prefix, suffix] = pattern.split('*');
        if (suffix !== undefined && key.startsWith(prefix) && key.endsWith(suffix) && key.length >= prefix.length + suffix.length) {
          out.push(...collect(exports[pattern], key.slice(prefix.length, key.length - suffix.length)));
        }
      }
    }
  }
  if (!sub) out.push(...[manifest?.types, manifest?.typings, manifest?.module, manifest?.main].filter((v): v is string => typeof v === 'string'));
  return out;
}
const sourceExtensions = ['.ts', '.tsx', '.mts', '.cts', '.d.ts', '.js', '.jsx'];
function firstExisting(base: string): string | undefined {
  for (const ext of sourceExtensions) if (fs.existsSync(base + ext) && fs.statSync(base + ext).isFile()) return base + ext;
  for (const ext of sourceExtensions) if (fs.existsSync(path.join(base, 'index' + ext))) return path.join(base, 'index' + ext);
  return undefined;
}
const outputDirs = new Set(['dist', 'build', 'lib', 'out', 'esm', 'cjs']);
function workspaceSource(name: string, from: string): string | undefined {
  if (!name || name.startsWith('.') || name.startsWith('/') || name.includes(':')) return undefined;
  const parts = name.split('/');
  const scoped = name.startsWith('@');
  const pkg = scoped ? parts.slice(0, 2).join('/') : parts[0];
  const sub = parts.slice(scoped ? 2 : 1).join('/');
  const dir = packageDir(pkg, from);
  if (!dir) return undefined;
  const configFile = path.join(dir, 'tsconfig.json');
  const config = fs.existsSync(configFile) ? parseConfig(configFile) : undefined;
  const outDir = config?.options.outDir, rootDir = config?.options.rootDir;
  for (const target of entryTargets(manifestOf(dir), sub)) {
    const stem = path.resolve(dir, target).replace(/(\.d)?\.[mc]?[jt]sx?$/, '');
    const bases: string[] = [];
    if (outDir && (stem + '/').startsWith(path.resolve(outDir) + '/')) bases.push(path.join(rootDir ? path.resolve(rootDir) : path.join(dir, 'src'), path.relative(path.resolve(outDir), stem)));
    const [first, ...rest] = path.relative(dir, stem).split(path.sep);
    if (outputDirs.has(first)) bases.push(path.join(dir, 'src', ...rest));
    bases.push(stem);
    for (const base of bases) {
      const found = firstExisting(base);
      if (found && inCheckout(found)) return found;
    }
  }
  const found = sub ? firstExisting(path.join(dir, 'src', sub)) ?? firstExisting(path.join(dir, sub)) : firstExisting(path.join(dir, 'src', 'index'));
  return found && inCheckout(found) ? found : undefined;
}
function extensionOf(file: string): ts.Extension {
  if (file.endsWith('.d.ts')) return ts.Extension.Dts;
  if (file.endsWith('.d.mts')) return ts.Extension.Dmts;
  if (file.endsWith('.d.cts')) return ts.Extension.Dcts;
  const ext = path.extname(file);
  return ({ '.ts': ts.Extension.Ts, '.tsx': ts.Extension.Tsx, '.mts': ts.Extension.Mts, '.cts': ts.Extension.Cts, '.js': ts.Extension.Js, '.jsx': ts.Extension.Jsx } as Record<string, ts.Extension>)[ext] ?? ts.Extension.Ts;
}

// Projects: every file is analysed with the options of the tsconfig (or
// jsconfig) that governs it, as an editor would: the nearest one above it
// that includes it, one of the projects that nearest one references, or the
// nearest one when none includes it.
const configs = new Map<string, ts.ParsedCommandLine | undefined>();
function parseConfig(file: string): ts.ParsedCommandLine | undefined {
  if (configs.has(file)) return configs.get(file);
  let parsed: ts.ParsedCommandLine | undefined;
  try {
    parsed = ts.getParsedCommandLineOfConfigFile(file, { ignoreDeprecations: '6.0' } as ts.CompilerOptions, {
      ...ts.sys, onUnRecoverableConfigFileDiagnostic: () => {},
    });
  } catch { parsed = undefined; }
  configs.set(file, parsed);
  return parsed;
}
const includedSets = new Map<string, Set<string>>();
function includes(configFile: string, parsed: ts.ParsedCommandLine, file: string): boolean {
  let set = includedSets.get(configFile);
  if (!set) includedSets.set(configFile, set = new Set(parsed.fileNames.map(f => path.resolve(f))));
  return set.has(file);
}
function referencePath(from: string, ref: ts.ProjectReference): string {
  const p = path.resolve(path.dirname(from), ref.path);
  return p.endsWith('.json') ? p : path.join(p, 'tsconfig.json');
}
function configFor(file: string): string | undefined {
  let nearest: string | undefined;
  for (let dir = path.dirname(file); dir === root || dir.startsWith(root + '/'); dir = path.dirname(dir)) {
    for (const name of ['tsconfig.json', 'jsconfig.json']) {
      const candidate = path.join(dir, name);
      if (!fs.existsSync(candidate)) continue;
      nearest ??= candidate;
      const parsed = parseConfig(candidate);
      if (parsed && includes(candidate, parsed, file)) return candidate;
      for (const ref of parsed?.projectReferences ?? []) {
        const refFile = referencePath(candidate, ref);
        const refParsed = fs.existsSync(refFile) ? parseConfig(refFile) : undefined;
        if (refParsed && includes(refFile, refParsed, file)) return refFile;
      }
      break;
    }
    if (dir === root) break;
  }
  return nearest;
}

const defaults: ts.CompilerOptions = {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, moduleResolution: ts.ModuleResolutionKind.Bundler,
  esModuleInterop: true, allowSyntheticDefaultImports: true, resolveJsonModule: true,
  lib: ['lib.es2023.d.ts', 'lib.dom.d.ts', 'lib.dom.iterable.d.ts'],
};
// The analysis emits nothing and binds JavaScript too; everything else is
// the project's own setting.
function analysisOptions(base: ts.CompilerOptions): ts.CompilerOptions {
  const o: ts.CompilerOptions = { ...base };
  o.noEmit = true; o.skipLibCheck = true; o.allowJs = true; o.checkJs = false; o.maxNodeModuleJsDepth = 0;
  o.composite = false; o.incremental = false; o.declaration = false; o.declarationMap = false; o.sourceMap = false;
  o.emitDeclarationOnly = false; o.noResolve = false; (o as any).ignoreDeprecations = '6.0';
  delete o.tsBuildInfoFile; delete o.outFile; delete o.plugins;
  if (o.jsx === undefined) o.jsx = ts.JsxEmit.Preserve;
  return o;
}

// Declarations.
function nameText(node: ts.Node | undefined): string | undefined {
  if (!node) return undefined;
  if (ts.isIdentifier(node) || ts.isPrivateIdentifier(node) || ts.isStringLiteral(node) || ts.isNumericLiteral(node)) return node.text;
  if (ts.isComputedPropertyName(node)) return node.getText();
  return undefined;
}
function nameNodeOf(decl: ts.Declaration): ts.Node | undefined {
  if (ts.isConstructorDeclaration(decl)) return decl.getChildren().find(c => c.kind === ts.SyntaxKind.ConstructorKeyword);
  if ((ts.isArrowFunction(decl) || ts.isFunctionExpression(decl) || ts.isClassExpression(decl)) && !(decl as any).name) {
    const p = decl.parent;
    if (ts.isVariableDeclaration(p) || ts.isPropertyAssignment(p) || ts.isPropertyDeclaration(p)) return p.name;
    return undefined;
  }
  if (ts.isSourceFile(decl)) return undefined;
  return ts.getNameOfDeclaration(decl);
}
const typeDeclaration = (d: ts.Node) => ts.isClassLike(d) || ts.isInterfaceDeclaration(d) || ts.isTypeAliasDeclaration(d)
  || ts.isEnumDeclaration(d) || ts.isTypeParameterDeclaration(d) || ts.isModuleDeclaration(d);
const memberDeclaration = (d: ts.Node) => ts.isPropertySignature(d) || ts.isPropertyDeclaration(d) || ts.isMethodSignature(d)
  || ts.isMethodDeclaration(d) || ts.isGetAccessorDeclaration(d) || ts.isSetAccessorDeclaration(d) || ts.isPropertyAssignment(d)
  || ts.isShorthandPropertyAssignment(d) || ts.isEnumMember(d);
const functionLike = (d: ts.Node) => ts.isFunctionDeclaration(d) || ts.isMethodDeclaration(d) || ts.isConstructorDeclaration(d);

// An overload signature and its implementation are one runtime entity: a
// use binds to the implementation when the checkout has one.
function implementation(decl: ts.Declaration, checker: ts.TypeChecker): ts.Declaration {
  if (!functionLike(decl) || (decl as ts.FunctionLikeDeclaration).body || decl.getSourceFile().isDeclarationFile) return decl;
  const name = ts.isConstructorDeclaration(decl) ? undefined : ts.getNameOfDeclaration(decl);
  const symbol = name ? checker.getSymbolAtLocation(name) : (decl.parent as any)?.symbol?.members?.get('__constructor');
  const body = symbol?.declarations?.find((d: ts.Declaration) => functionLike(d) && (d as ts.FunctionLikeDeclaration).body);
  return body ?? decl;
}

function resolveAlias(symbol: ts.Symbol | undefined, checker: ts.TypeChecker): ts.Symbol | undefined {
  if (symbol && symbol.flags & ts.SymbolFlags.Alias) {
    try {
      const aliased = checker.getAliasedSymbol(symbol);
      if (aliased && aliased.declarations?.length) return aliased;
      return undefined;
    } catch { return undefined; }
  }
  return symbol;
}

// The declarations a symbol stands for at a value or type use: its value
// declaration, the implementation of an overloaded function, the type
// declaration of a merged type, or every constituent's member of a union.
function declarationsOf(symbol: ts.Symbol | undefined, use: 'value' | 'type', checker: ts.TypeChecker): ts.Declaration[] {
  symbol = resolveAlias(symbol, checker);
  const decls = symbol?.declarations ?? [];
  if (!decls.length) return [];
  // A module or namespace import used as a value is no declaration a
  // reference can bind to.
  if (decls.every(d => ts.isSourceFile(d))) return [];
  if (use === 'type') {
    const types = decls.filter(typeDeclaration);
    return [types[0] ?? decls[0]];
  }
  if (decls.length > 1 && decls.every(memberDeclaration)) {
    const parents = new Set(decls.map(d => d.parent));
    if (parents.size > 1) {
      // A member of each union constituent: one member when they all
      // declare again what a common base declares (the states of a
      // discriminated union narrowing its base's data), each otherwise.
      const common = commonDeclaration(decls, symbol!.name, checker);
      return common ? [common] : decls;
    }
  }
  const bodies = decls.filter(d => functionLike(d) && (d as ts.FunctionLikeDeclaration).body);
  if (bodies.length === 1) return [bodies[0]];
  return [symbol!.valueDeclaration ?? decls[0]];
}

// ancestry is a member's declaration followed by the declarations of the
// same-named member in the bases of the type declaring it, nearest first.
function ancestry(decl: ts.Declaration, name: string, checker: ts.TypeChecker, depth = 0): ts.Declaration[] {
  const out: ts.Declaration[] = [decl];
  const owner = decl.parent;
  if (depth > 16 || !owner || !(ts.isInterfaceDeclaration(owner) || ts.isClassLike(owner)) || !owner.name) return out;
  const ownerSymbol = checker.getSymbolAtLocation(owner.name);
  if (!ownerSymbol) return out;
  const type = checker.getDeclaredTypeOfSymbol(ownerSymbol);
  if (!(type.flags & ts.TypeFlags.Object)) return out;
  for (const base of checker.getBaseTypes(type as ts.InterfaceType) ?? []) {
    const property = checker.getPropertyOfType(base, name);
    for (const d of property?.declarations ?? []) out.push(...ancestry(d, name, checker, depth + 1));
  }
  return out;
}
function commonDeclaration(decls: ts.Declaration[], name: string, checker: ts.TypeChecker): ts.Declaration | undefined {
  try {
    const [first, ...rest] = decls.map(d => ancestry(d, name, checker));
    return first.find(d => rest.every(list => list.includes(d)));
  } catch { return undefined; }
}

function calleeName(callee: ts.Node): ts.Node | undefined {
  while (ts.isParenthesizedExpression(callee) || ts.isNonNullExpression(callee) || ts.isAsExpression(callee)) callee = callee.expression;
  if (ts.isIdentifier(callee) || ts.isPrivateIdentifier(callee)) return callee;
  if (ts.isPropertyAccessExpression(callee)) return callee.name;
  if (ts.isElementAccessExpression(callee)) return callee.argumentExpression;
  if (callee.kind === ts.SyntaxKind.SuperKeyword) return callee;
  return undefined;
}

function callTargets(call: ts.CallExpression | ts.NewExpression | ts.TaggedTemplateExpression | ts.Decorator, checker: ts.TypeChecker): ts.Declaration[] {
  let decl: ts.Declaration | undefined;
  try {
    decl = checker.getResolvedSignature(call as ts.CallLikeExpression)?.declaration as ts.Declaration | undefined;
  } catch { decl = undefined; }
  if (decl && !ts.isJSDocSignature(decl) && describable(decl)) return [implementation(decl, checker)];
  const callee = ts.isDecorator(call) ? call.expression : ts.isTaggedTemplateExpression(call) ? call.tag : call.expression;
  const name = callee && calleeName(callee);
  if (!name) return [];
  return declarationsOf(checker.getSymbolAtLocation(name), 'value', checker);
}

function overrideTargets(name: ts.Node, checker: ts.TypeChecker): ts.Declaration[] {
  const member = name.parent;
  const cls = member?.parent;
  if (!cls || !ts.isClassLike(cls) || !(ts.isMethodDeclaration(member) || ts.isPropertyDeclaration(member) || ts.isGetAccessorDeclaration(member) || ts.isSetAccessorDeclaration(member))) return [];
  if (ts.getCombinedModifierFlags(member as ts.Declaration) & ts.ModifierFlags.Static) return [];
  if (!cls.heritageClauses?.some(h => h.token === ts.SyntaxKind.ExtendsKeyword)) return [];
  const own = cls.name ? checker.getSymbolAtLocation(cls.name) : (cls as any).symbol;
  if (!own) return [];
  const text = nameText(name);
  if (!text) return [];
  const type = checker.getDeclaredTypeOfSymbol(own);
  for (const base of checker.getBaseTypes(type as ts.InterfaceType)) {
    const property = checker.getPropertyOfType(base, text);
    const decls = property ? declarationsOf(property, 'value', checker) : [];
    if (decls.length) return [decls[0]];
  }
  return [];
}

function targetsFor(site: Site, nodes: ts.Node[], checker: ts.TypeChecker): ts.Declaration[] {
  switch (site.kind) {
    case 'call': {
      const call = nodes.find(n => ts.isCallExpression(n) || ts.isNewExpression(n) || ts.isTaggedTemplateExpression(n) || ts.isDecorator(n));
      return call ? callTargets(call as any, checker) : [];
    }
    case 'member': {
      const access = nodes.find(n => ts.isPropertyAccessExpression(n) || ts.isQualifiedName(n) || ts.isElementAccessExpression(n));
      if (!access) return [];
      const name = ts.isPropertyAccessExpression(access) ? access.name : ts.isQualifiedName(access) ? access.right : (access as ts.ElementAccessExpression).argumentExpression;
      return declarationsOf(checker.getSymbolAtLocation(name), 'value', checker);
    }
    case 'name': {
      const id = nodes.find(n => ts.isIdentifier(n) || ts.isPrivateIdentifier(n));
      if (!id) return [];
      const parent = id.parent;
      const symbol = parent && ts.isShorthandPropertyAssignment(parent) && parent.name === id
        ? checker.getShorthandAssignmentValueSymbol(parent) : checker.getSymbolAtLocation(id);
      return declarationsOf(symbol, 'value', checker);
    }
    case 'type': {
      const n = nodes.find(n => ts.isTypeReferenceNode(n) || ts.isExpressionWithTypeArguments(n)) ?? nodes.find(n => ts.isIdentifier(n) || ts.isQualifiedName(n) || ts.isPropertyAccessExpression(n));
      if (!n) return [];
      let name: ts.Node = n;
      if (ts.isTypeReferenceNode(name)) name = name.typeName;
      if (ts.isExpressionWithTypeArguments(name)) name = name.expression;
      if (ts.isQualifiedName(name)) name = name.right;
      if (ts.isPropertyAccessExpression(name)) name = name.name;
      return declarationsOf(checker.getSymbolAtLocation(name), 'type', checker);
    }
    case 'decorator': {
      const d = nodes.find(ts.isDecorator);
      if (!d) return [];
      let e: ts.Expression = d.expression;
      if (ts.isCallExpression(e)) e = e.expression;
      const name = calleeName(e);
      return name ? declarationsOf(checker.getSymbolAtLocation(name), 'value', checker) : [];
    }
    case 'override': {
      const name = nodes.find(n => (ts.isIdentifier(n) || ts.isPrivateIdentifier(n) || ts.isStringLiteral(n)) && n.parent && ts.getNameOfDeclaration(n.parent as ts.Declaration) === n);
      return name ? overrideTargets(name, checker) : [];
    }
    default:
      return [];
  }
}

// Naming what is outside the checkout.
function packageRoot(file: string): { name: string; dir: string } | undefined {
  const at = file.lastIndexOf('/node_modules/');
  if (at < 0) return undefined;
  const rest = file.slice(at + '/node_modules/'.length).split('/');
  const name = rest[0].startsWith('@') ? rest[0] + '/' + rest[1] : rest[0];
  return { name, dir: file.slice(0, at + '/node_modules/'.length) + name };
}
// A declaration package (@types/react) describes the runtime package
// (react); @types/scope__name describes @scope/name.
function runtimePackage(name: string): string {
  if (!name.startsWith('@types/')) return name;
  const bare = name.slice('@types/'.length);
  return bare.includes('__') ? '@' + bare.replace('__', '/') : bare;
}
// The entity a module exports as itself (export = React) is the module:
// its members are named without it (react#useState, not React.useState).
const exportEquals = new Map<ts.Node, string | undefined>();
function exportedAsModule(container: ts.Node): string | undefined {
  if (exportEquals.has(container)) return exportEquals.get(container);
  let name: string | undefined;
  const statements: readonly ts.Statement[] = ts.isSourceFile(container) ? container.statements
    : (ts.isModuleDeclaration(container) && container.body && ts.isModuleBlock(container.body)) ? container.body.statements : [];
  for (const s of statements) {
    if (ts.isExportAssignment(s) && s.isExportEquals && ts.isIdentifier(s.expression)) name = s.expression.text;
  }
  exportEquals.set(container, name);
  return name;
}

function describe(decl: ts.Declaration): Target | undefined {
  const nameNode = nameNodeOf(decl);
  const name = nameText(nameNode) ?? (ts.isConstructorDeclaration(decl) ? 'constructor' : undefined);
  if (!name) return undefined;
  const sf = decl.getSourceFile();
  const file = path.resolve(sf.fileName);
  if (inCheckout(file)) {
    if (!nameNode) return undefined;
    const bytes = bytesOf(sf);
    const target: Target = { module: 'source', name, path: file, start: bytes[nameNode.getStart(sf)], end: bytes[nameNode.end] };
    // A member of an object or type literal (a zod schema's field, an inline
    // props type) has no declaration of its own in the graph: the enclosing
    // named declarations, innermost first, with the member's path below
    // each, let the resolver name it as a member of one.
    if (memberDeclaration(decl) && !ts.isClassLike(decl.parent) && !ts.isInterfaceDeclaration(decl.parent) && !ts.isEnumDeclaration(decl.parent)) {
      const owners: Owner[] = [];
      const below = [name];
      for (let n: ts.Node | undefined = decl.parent; n && !ts.isSourceFile(n) && owners.length < 8; n = n.parent) {
        const named = (ts.isVariableDeclaration(n) || ts.isParameter(n) || ts.isPropertyAssignment(n) || ts.isPropertySignature(n)
          || ts.isPropertyDeclaration(n) || ts.isFunctionDeclaration(n) || ts.isMethodDeclaration(n) || ts.isClassLike(n)
          || ts.isInterfaceDeclaration(n) || ts.isTypeAliasDeclaration(n) || ts.isBindingElement(n) || ts.isGetAccessorDeclaration(n)) ? n.name : undefined;
        const text = nameText(named);
        if (!named || !text) continue;
        owners.push({ start: bytes[named.getStart(sf)], end: bytes[named.end], member: below.join('.') });
        below.unshift(text);
      }
      if (owners.length) target.owners = owners;
    }
    return target;
  }
  const names = [name];
  let container: ts.Node = sf;
  let moduleName: string | undefined;
  let global = !ts.isExternalModule(sf);
  for (let n: ts.Node | undefined = decl.parent; n && !ts.isSourceFile(n); n = n.parent) {
    if (ts.isModuleDeclaration(n)) {
      if (ts.isStringLiteral(n.name)) { moduleName = n.name.text; container = n; global = false; break; }
      if (n.flags & ts.NodeFlags.GlobalAugmentation) { global = true; container = n; break; }
      names.unshift(n.name.text);
      continue;
    }
    if (ts.isClassLike(n) || ts.isInterfaceDeclaration(n) || ts.isEnumDeclaration(n) || ts.isTypeAliasDeclaration(n) || ts.isFunctionDeclaration(n)) {
      const t = nameText(n.name);
      if (t) names.unshift(t);
      continue;
    }
    if (ts.isVariableDeclaration(n) && ts.isIdentifier(n.name)) names.unshift(n.name.text);
  }
  const pkg = packageRoot(file);
  if (!pkg && (global || !moduleName)) return { module: 'lib', name, qualified: names.join('.') };
  if (!global) {
    const self = exportedAsModule(container);
    if (self && names[0] === self) names.shift();
  }
  return { module: 'package', name, pkg: moduleName ?? runtimePackage(pkg!.name), qualified: names.join('.') };
}

function describable(decl: ts.Declaration): boolean {
  return nameText(nameNodeOf(decl)) !== undefined || ts.isConstructorDeclaration(decl);
}

// Nodes by byte span, for the sites of one file.
function nodesBySpan(sf: ts.SourceFile): Map<string, ts.Node[]> {
  const bytes = bytesOf(sf);
  const map = new Map<string, ts.Node[]>();
  const visit = (node: ts.Node) => {
    const key = bytes[node.getStart(sf)] + ':' + bytes[node.end];
    const list = map.get(key);
    if (list) list.push(node); else map.set(key, [node]);
    ts.forEachChild(node, visit);
  };
  ts.forEachChild(sf, visit);
  return map;
}

emit({ event: 'hello', protocol: 1, typescript: ts.version, context: request.context });
const files = request.files.map(f => ({ ...f, path: path.resolve(f.path) }));
const groups = new Map<string, typeof files>();
for (const f of files) {
  const config = configFor(f.path) ?? '';
  const group = groups.get(config);
  if (group) group.push(f); else groups.set(config, [f]);
}
let programs = 0, sites = 0, answered = 0, failures = 0;
for (const [config, group] of [...groups].sort(([a], [b]) => a.localeCompare(b))) {
  const parsed = config ? parseConfig(config) : undefined;
  const options = analysisOptions(parsed?.options ?? defaults);
  // Global declarations of the project (env.d.ts, types/*.d.ts) shape the
  // types its files see.
  const ambient = (parsed?.fileNames ?? []).map(f => path.resolve(f)).filter(f => /\.d\.[mc]?ts$/.test(f) && inCheckout(f));
  const program = ts.createProgram({ rootNames: [...new Set([...group.map(f => f.path), ...ambient])], options, host: hostFor(options) });
  const checker = program.getTypeChecker();
  programs++;
  for (const f of group) {
    const sf = program.getSourceFile(f.path);
    if (sf && f.sites?.length) {
      const nodes = nodesBySpan(sf);
      for (const site of f.sites) {
        sites++;
        let targets: Target[] = [];
        try {
          const found = targetsFor(site, nodes.get(site.start + ':' + site.end) ?? [], checker);
          const seen = new Set<string>();
          for (const d of found) {
            const t = describe(d);
            const key = t && JSON.stringify(t);
            if (t && !seen.has(key!)) { seen.add(key!); targets.push(t); }
          }
        } catch {
          failures++;
          targets = [];
        }
        if (targets.length) {
          answered++;
          emit({ event: 'lookup', file: f.id, site: site.id, targets });
        }
      }
    }
    emit({ event: 'file_done', file: f.id });
  }
}
emit({ event: 'done', context: request.context, programs, sites, answered, failures, seconds: (Date.now() - started) / 1000 });
flush();
