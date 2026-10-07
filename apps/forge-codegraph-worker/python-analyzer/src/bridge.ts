// All Pyright-internal API dependencies live here. Upgrade only with the
// analyzer contract tests. This process never executes a Python interpreter.
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { Program } from 'pyright/analyzer/program';
import { ImportResolver } from 'pyright/analyzer/importResolver';
import { ParseTreeWalker } from 'pyright/analyzer/parseTreeWalker';
import { DeclarationType } from 'pyright/analyzer/declaration';
import { isFunction, isOverloaded, isClass, isClassInstance, isUnion, isAnyOrUnknown, OverloadedType } from 'pyright/analyzer/types';
import { ConfigOptions, ExecutionEnvironment } from 'pyright/common/configOptions';
import { NullConsole } from 'pyright/common/console';
import { NoAccessHost } from 'pyright/common/host';
import { RealTempFile, createFromRealFileSystem } from 'pyright/common/realFileSystem';
import { createServiceProvider } from 'pyright/common/serviceProviderExtensions';
import { UriEx } from 'pyright/common/uri/uriUtils';
import { ParseNodeType } from 'pyright/parser/parseNodes';

const request = JSON.parse(fs.readFileSync(0, 'utf8'));
if (request.protocol !== 1) throw new Error('unsupported protocol');
const temp = new RealTempFile();
const realFS = createFromRealFileSystem(temp);
const service = createServiceProvider(realFS, new NullConsole(), temp);
const config = new ConfigOptions(UriEx.file(request.root));
config.defaultPythonVersion = { major: 3, minor: Number(request.version.split('.')[1]) };
config.defaultPythonPlatform = request.platform;
config.typeshedPath = UriEx.file(path.join(__dirname, 'typeshed-fallback'));
config.defaultExtraPaths = request.roots.map((p: string) => UriEx.file(p));
config.useLibraryCodeForTypes = true;
// The projects of a monorepo, innermost first: Pyright analyses a file in
// the first environment whose root holds it.
for (const env of request.environments ?? []) {
  config.executionEnvironments.push(new ExecutionEnvironment('project', UriEx.file(env.root), config.diagnosticRuleSet,
    config.defaultPythonVersion, config.defaultPythonPlatform, env.extraPaths.map((p: string) => UriEx.file(p))));
}
const importRoots: string[] = [...new Set<string>([request.root, ...(request.roots ?? []),
  ...(request.environments ?? []).flatMap((env: any) => [env.root, ...env.extraPaths])])];
class InventoryHost extends NoAccessHost {
  getPythonSearchPaths() { return { paths: request.dependencies.map((p: string) => UriEx.file(p)), prefix: undefined }; }
}
const imports = new ImportResolver(service, config, new InventoryHost());
const program = new Program(imports, config, service);
const emit = (event: any) => fs.writeSync(1, JSON.stringify(event) + '\n');
const positionCache = new Map<string, { bytes: Uint32Array; lines: number[]; digest: string }>();
function positions(text: string) {
  let result = positionCache.get(text);
  if (result) return result;
  const indices = new Uint32Array(text.length + 1), lines = [0];
  let byte = 0;
  for (let i = 0; i < text.length;) {
    const point = text.codePointAt(i)!;
    const width = point > 0xffff ? 2 : 1;
    indices[i] = byte;
    if (width === 2) indices[i + 1] = byte;
    byte += point <= 0x7f ? 1 : point <= 0x7ff ? 2 : point <= 0xffff ? 3 : 4;
    if (text[i] === '\n' || (text[i] === '\r' && text[i + 1] !== '\n')) lines.push(i + 1);
    i += width;
  }
  indices[text.length] = byte;
  result = { bytes: indices, lines, digest: createHash('sha256').update(text).digest('hex') };
  positionCache.set(text, result);
  return result;
}
const bytes = (text: string, offset: number) => positions(text).bytes[offset];
const contentCache = new Map<string, string>();
function content(p: string): string {
  if (!contentCache.has(p)) contentCache.set(p, fs.readFileSync(p, 'utf8'));
  return contentCache.get(p)!;
}
function offset(text: string, position: any): number {
  return bytes(text, positions(text).lines[position.line] + position.character);
}
const evaluator = program.evaluator!;
// intrinsicTarget is the typeshed declaration an implicit name stands for:
// a module's __name__ or __file__ is ModuleType's attribute, __qualname__
// type's, and a class body's __doc__ or __module__ and a method's __class__
// object's. A name typeshed does not declare there stays unmapped.
function intrinsicTarget(decl: any): any {
  const node = decl.node;
  const owner = node?.nodeType === ParseNodeType.Module ? evaluator.getTypingType(node, 'ModuleType')
    : decl.name === '__qualname__' ? evaluator.getTypeClassType()
    : evaluator.getBuiltInType(node, 'object');
  if (!owner || !isClass(owner)) return undefined;
  // Pyright gives typeshed's classes implicit names of their own too.
  const declared = (owner.shared.fields.get(decl.name)?.getDeclarations() ?? []).find((d: any) => d.type !== DeclarationType.Intrinsic);
  return declared && target(declared);
}
function target(decl: any): any {
  if (decl?.type === DeclarationType.Intrinsic) return intrinsicTarget(decl);
  if (!decl || decl.type === DeclarationType.Alias) decl = decl && evaluator.resolveAliasDeclaration(decl, true);
  if (!decl || !decl.uri || !decl.uri.getFilePath()) return undefined;
  const p = decl.uri.getFilePath();
  if (!fs.existsSync(p) || !fs.statSync(p).isFile()) return undefined;
  const text = content(p);
  if (decl.type === DeclarationType.Alias && !decl.symbolName) {
    const moduleName = imports.getModuleNameForImport(decl.uri, config.findExecEnvironment(decl.uri)).moduleName;
    return { path: p, start: 0, end: 0, name: moduleName.split('.').at(-1), kind: 'namespace',
      qualified: moduleName, role: 'module', sha256: positions(text).digest };
  }
  const nameNode = decl.node?.d?.name ?? (decl.node?.nodeType === ParseNodeType.Name ? decl.node
    : decl.node?.nodeType === ParseNodeType.TypeAnnotation ? decl.node.d.valueExpr : undefined);
  const name = nameNode?.d?.value ?? decl.name ?? '';
  const kind = decl.type === DeclarationType.Class || decl.type === DeclarationType.SpecialBuiltInClass ? 'class'
    : decl.type === DeclarationType.Function ? (decl.isMethod ? 'method' : 'function')
    : decl.type === DeclarationType.Param ? 'parameter'
    : decl.type === DeclarationType.TypeParam ? 'type_parameter'
    : decl.type === DeclarationType.TypeAlias ? 'type_alias' : 'variable';
  const owners: string[] = [];
  for (let n = decl.node?.parent; n; n = n.parent) {
    if (n.nodeType === ParseNodeType.Class || n.nodeType === ParseNodeType.Function) owners.unshift(n.d.name.d.value);
  }
  // Declaration ranges are authoritative across aliases and synthetic nodes,
  // except that Pyright ranges a parameter over its annotation and default
  // and a type parameter over its whole list; the IR declares both by name.
  const named = (decl.type === DeclarationType.Param || decl.type === DeclarationType.TypeParam) && nameNode;
  const result: any = { path: p,
    start: named ? bytes(text, nameNode.start) : offset(text, decl.range.start),
    end: named ? bytes(text, nameNode.start + nameNode.length) : offset(text, decl.range.end),
    name, kind, qualified: [decl.moduleName, ...owners, name].filter(Boolean).join('.'),
    sha256: positions(text).digest };
  if (decl.type === DeclarationType.Variable && decl.node?.parent?.nodeType === ParseNodeType.MemberAccess) {
    const receiver = evaluator.makeTopLevelTypeVarsConcrete(evaluator.getTypeOfExpression(decl.node.parent.d.leftExpr).type);
    if (isClass(receiver)) {
      const owner = target(receiver.shared.declaration);
      if (owner) { result.derivedOwner = owner; result.kind = 'field'; result.qualified = owner.qualified + '.' + name; result.role = 'instance_attribute'; }
    }
  }
  return result;
}
// unresolvedPackage is the module an import names when Pyright cannot resolve
// it and it is not the repository's own (a package that is not installed):
// the dotted name the imported binding stands for, or undefined.
function unresolvedPackage(decl: any): string | undefined {
  if (!decl || decl.type !== DeclarationType.Alias || decl.isNativeLib) return undefined;
  if (!decl.isUnresolved) {
    // An import Pyright could not find resolves to a placeholder with no file.
    const resolved = evaluator.resolveAliasDeclaration(decl, true);
    if (resolved && resolved.uri?.getFilePath() && !resolved.isUnresolved) return undefined;
  }
  const node = decl.node;
  let moduleNode: any, wholeModule = true;
  if (node?.nodeType === ParseNodeType.ImportAs) {
    // import a.b.c binds a; import a.b.c as abc binds a.b.c.
    moduleNode = node.d.module; wholeModule = !!node.d.alias;
  } else if (node?.nodeType === ParseNodeType.ImportFromAs) moduleNode = node.parent?.d?.module;
  else if (node?.nodeType === ParseNodeType.ImportFrom) moduleNode = node.d.module;
  if (!moduleNode || moduleNode.d.leadingDots > 0) return undefined;
  const parts: string[] = (moduleNode.d.nameParts ?? []).map((p: any) => p.d.value);
  const symbol = node?.nodeType === ParseNodeType.ImportFromAs ? node.d.name?.d?.value : undefined;
  if (!parts.length || localModule(parts, symbol)) return undefined;
  return wholeModule ? parts.join('.') : parts[0];
}
// localModule reports a module the repository itself has under one of its
// import roots or projects: an import of it Pyright could not resolve is a
// configuration problem, not a package. A module file or a regular package
// named like the top-level name is the repository's; a namespace directory
// is only when it holds what is imported from it, so a directory of scripts
// named like a package (alembic migrations) does not hide the package.
function localModule(parts: string[], symbol?: string): boolean {
  const isFile = (p: string) => fs.existsSync(p) && fs.statSync(p).isFile();
  const isDir = (p: string) => fs.existsSync(p) && fs.statSync(p).isDirectory();
  const module = (p: string) => isFile(p + '.py') || isFile(p + '.pyi');
  return importRoots.some((root: string) => {
    const top = path.join(root, parts[0]);
    if (module(top) || isFile(path.join(top, '__init__.py')) || isFile(path.join(top, '__init__.pyi'))) return true;
    if (!isDir(top)) return false;
    const names = symbol === undefined ? parts : [...parts, symbol];
    const full = path.join(root, ...names);
    return names.length === 1 || module(full) || isDir(full);
  });
}
// externalPath names what an expression takes from an unresolved package, as
// a dotted path from the module: fastapi.FastAPI().get for app.get when app =
// FastAPI(). Undefined when the expression does not come from one.
function externalPath(expr: any, depth = 0): string | undefined {
  if (!expr || depth > 24) return undefined;
  switch (expr.nodeType) {
    case ParseNodeType.Name: {
      const decls = evaluator.getDeclInfoForNameNode(expr)?.decls ?? [];
      for (const d of decls) {
        if (d.type !== DeclarationType.Alias) continue;
        const module = unresolvedPackage(d);
        if (module !== undefined) return d.symbolName ? module + '.' + d.symbolName : module;
      }
      // x = <value>, assigned once: the value's path; the variable of a
      // for loop is one of the iterable's elements, and the target of a
      // with statement what its expression opens.
      if (decls.length === 1 && decls[0].type === DeclarationType.Variable && decls[0].inferredTypeSource) {
        const source = decls[0].inferredTypeSource;
        if (source.nodeType === ParseNodeType.For) {
          const iterable = externalPath(source.d.iterableExpr, depth + 1);
          return iterable && iterable + '[]';
        }
        if (source.nodeType === ParseNodeType.WithItem) return externalPath(source.d.expr, depth + 1);
        return externalPath(source, depth + 1);
      }
      return undefined;
    }
    case ParseNodeType.MemberAccess: {
      const base = externalPath(expr.d.leftExpr, depth + 1);
      return base && base + '.' + expr.d.member.d.value;
    }
    case ParseNodeType.Call: {
      const base = externalPath(expr.d.leftExpr, depth + 1);
      if (base) return base + '()';
      // A repository function that returns something from a package
      // (def get_logger(name): return structlog.get_logger(name)): the
      // call's result is what it returns.
      if (expr.d.leftExpr.nodeType !== ParseNodeType.Name) return undefined;
      for (const d of evaluator.getDeclInfoForNameNode(expr.d.leftExpr)?.decls ?? []) {
        const fn = d.type === DeclarationType.Alias ? evaluator.resolveAliasDeclaration(d, true) : d;
        if (fn?.type !== DeclarationType.Function) continue;
        for (const ret of fn.returnStatements ?? []) {
          const found = ret.d.expr && externalPath(ret.d.expr, depth + 1);
          if (found) return found;
        }
      }
      return undefined;
    }
    case ParseNodeType.Await:
      return externalPath(expr.d.expr, depth + 1);
  }
  return undefined;
}
// externalBaseMember names a member a class does not declare after the
// first of its bases, or of its bases' bases, that comes from an unresolved
// package: beanie.Document.find_one for User.find_one when User(Document).
function externalBaseMember(access: any): string | undefined {
  const receiver = evaluator.getTypeOfExpression(access.d.leftExpr).type;
  if (!receiver || !(isClass(receiver) || isClassInstance(receiver))) return undefined;
  const seen = new Set<any>();
  const visit = (cls: any, depth: number): string | undefined => {
    const node = cls?.shared?.declaration?.node;
    if (!node || node.nodeType !== ParseNodeType.Class || depth > 8 || seen.has(node)) return undefined;
    seen.add(node);
    for (const arg of node.d.arguments ?? []) {
      if (arg.d.name) continue; // metaclass=, total=, ...
      const base = externalPath(arg.d.valueExpr);
      if (base) return base;
    }
    for (const base of cls.shared.baseClasses ?? []) {
      if (!isClass(base)) continue;
      const found = visit(base, depth + 1);
      if (found) return found;
    }
    return undefined;
  };
  const base = visit(receiver, 0);
  return base && base + '.' + access.d.member.d.value;
}
function externalTarget(qualified: string): any {
  const name = qualified.slice(qualified.lastIndexOf('.') + 1);
  return { external: true, path: '', start: 0, end: 0, name, kind: 'variable', qualified, sha256: '' };
}
// oneDefinitionEach keeps one target per definition: the @overload stubs of a
// function and its implementation, or a name defined again in another branch,
// are one runtime entity. The last declaration is kept: an implementation
// follows its overloads.
function oneDefinitionEach(targets: any[]): any[] {
  const byDefinition = new Map<string, any>();
  for (const t of targets) byDefinition.set(`${t.path}\u0000${t.qualified ?? t.name}\u0000${t.kind}\u0000${t.role ?? ''}`, t);
  return [...byDefinition.values()];
}
// oneVariable keeps one declaration of a variable Pyright lists every
// assignment of where code flow cannot say which reaches the name (a
// closure's captured variable, a parameter assigned again, a nonlocal or
// global one): they are one variable, which its first declaration stands
// for. A class attribute also assigned through self is the class's
// declaration; members of different classes stay alternatives.
function oneVariable(node: any, decls: any[]): any[] {
  if (decls.length < 2 || !decls.every((d: any) => d.type === DeclarationType.Variable || d.type === DeclarationType.Param)) return decls;
  const member = node.parent?.nodeType === ParseNodeType.MemberAccess && node.parent.d.member === node;
  if (!member) return [decls[0]];
  const targets = decls.map(target);
  if (targets.some((t: any) => !t) || new Set(targets.map((t: any) => t.path + '\u0000' + t.qualified)).size !== 1) return decls;
  return [decls.find((_: any, i: number) => !targets[i].derivedOwner) ?? decls[0]];
}
// decoratedDefinitions are the function definitions a called name or member
// stands for when every one of them is decorated: a call runs the
// definition whatever callable the decorator returns (contextmanager,
// lru_cache, a ParamSpec wrapper). Overloads keep the overload resolution.
function decoratedDefinitions(expr: any): any[] {
  const name = expr.nodeType === ParseNodeType.Name ? expr : expr.nodeType === ParseNodeType.MemberAccess ? expr.d.member : undefined;
  if (!name) return [];
  const decls = (evaluator.getDeclInfoForNameNode(name)?.decls ?? []).map((d: any) => evaluator.resolveAliasDeclaration(d, true) ?? d);
  const decorated = (d: any) => d?.type === DeclarationType.Function && d.node?.d?.decorators?.length > 0
    && !d.node.d.decorators.some((dec: any) => {
      const e = dec.d.expr;
      return (e.nodeType === ParseNodeType.Name ? e.d.value : e.nodeType === ParseNodeType.MemberAccess ? e.d.member.d.value : '') === 'overload';
    });
  return decls.length && decls.every(decorated) ? decls : [];
}
function callableTargets(type: any, call: any, out: any[], state: { unknown: boolean }, depth = 0) {
  if (!type || depth > 16 || isAnyOrUnknown(type)) { state.unknown = true; return; }
  if (isUnion(type)) { for (const t of type.priv.subtypes) callableTargets(t, call, out, state, depth + 1); return; }
  if (isFunction(type)) {
    const t = target(type.shared.declaration);
    if (t) out.push(t);
    else {
      const owner = target(type.shared.methodClass?.shared.declaration);
      if (owner && type.shared.name) out.push({ ...owner, name: type.shared.name, kind: 'method',
        qualified: owner.qualified + '.' + type.shared.name, role: 'synthesized_member', derivedOwner: owner });
      else state.unknown = true;
    }
    return;
  }
  if (isOverloaded(type)) {
    const implementation = OverloadedType.getImplementation(type);
    if (implementation) { callableTargets(implementation, call, out, state, depth + 1); return; }
    const used = evaluator.getTypeOfExpression(call).overloadsUsedForCall;
    if (used?.length) for (const t of used) callableTargets(t, call, out, state, depth + 1);
    else state.unknown = true;
    return;
  }
  if (isClass(type) && !isClassInstance(type)) {
    const t = target(type.shared.declaration);
    if (t) out.push({ ...t, role: 'construction_contract' }); else state.unknown = true;
    return;
  }
  if (isClassInstance(type)) {
    const method = evaluator.getBoundMagicMethod(type, '__call__');
    if (method) { callableTargets(method, call, out, state, depth + 1); return; }
  }
  state.unknown = true;
}
function callableExpression(expr: any, call: any, out: any[], state: { unknown: boolean }, visited = new Set<any>()) {
  if (!expr || visited.has(expr) || visited.size > 32) { state.unknown = true; return; }
  const next = new Set(visited); next.add(expr);
  const definitions = decoratedDefinitions(expr);
  if (definitions.length) {
    for (const d of definitions) {
      const t = target(d);
      if (t) out.push(t); else state.unknown = true;
    }
    return;
  }
  if (expr.nodeType === ParseNodeType.Ternary) {
    callableExpression(expr.d.ifExpr, call, out, state, next);
    callableExpression(expr.d.elseExpr, call, out, state, next);
    return;
  }
  if (expr.nodeType === ParseNodeType.Name) {
    const decls = (evaluator.getDeclInfoForNameNode(expr)?.decls ?? []).map(d => evaluator.resolveAliasDeclaration(d, true) ?? d);
    const assignments = decls.filter(d => d.type === DeclarationType.Variable && d.inferredTypeSource);
    if (assignments.length) {
      for (const decl of assignments) {
        const source = decl.inferredTypeSource;
        if (source.nodeType === ParseNodeType.Assignment) callableExpression(source.d.rightExpr, call, out, state, next);
        else if ([ParseNodeType.Name, ParseNodeType.MemberAccess, ParseNodeType.Call, ParseNodeType.Ternary, ParseNodeType.Index, ParseNodeType.Lambda].includes(source.nodeType)) callableExpression(source, call, out, state, next);
        else state.unknown = true;
      }
      if (assignments.length !== decls.length) state.unknown = true;
      return;
    }
  }
  const result = evaluator.getTypeOfExpression(expr);
  state.unknown ||= !!result.isIncomplete || !!result.typeErrors;
  if (expr.nodeType === ParseNodeType.Index) {
    // Collection element types can coalesce distinct function identities.
    // Retain the analyzer's candidate but do not select an implementation.
    state.unknown = true;
  }
  if (expr.nodeType === ParseNodeType.Call && (isFunction(result.type) || isOverloaded(result.type))) {
    const factory = evaluator.getTypeOfExpression(expr.d.leftExpr).type;
    const returns = isFunction(factory) ? factory.shared.declaration?.returnStatements : undefined;
    if (returns?.length) {
      for (const ret of returns) callableExpression(ret.d.expr, call, out, state, next);
      return;
    }
    state.unknown = true;
  }
  callableTargets(result.type, call, out, state);
}
try {
  for (const file of request.files) {
    if (createHash('sha256').update(fs.readFileSync(file.path)).digest('hex') !== file.sha256) throw new Error('source digest mismatch: ' + file.path);
  }
  program.setTrackedFiles(request.files.map((f: any) => UriEx.file(f.path)));
  while (program.analyze()) { /* one coherent program including import cycles */ }
  emit({ event: 'hello', protocol: 1, pyright: '1.1.414', context: request.context });
  for (const file of request.files) {
    const uri = UriEx.file(file.path);
    const parsed = program.getParseResults(uri);
    if (!parsed) throw new Error('analyzer omitted file: ' + file.path);
    const text = content(file.path);
    const nodes = new Map<string, any[]>();
    class Walker extends ParseTreeWalker {
      visit(node: any) {
        const key = `${bytes(text, node.start)}:${bytes(text, node.start + node.length)}`;
        const list = nodes.get(key) ?? []; list.push(node); nodes.set(key, list);
        return super.visit(node);
      }
    }
    new Walker().walk(parsed.parserOutput.parseTree);
    const diagnostics = (program.getSourceFile(uri)?.getDiagnostics(config) ?? []).map((d: any) => ({
      start: offset(text, d.range.start), end: offset(text, d.range.end), message: d.message,
      code: d.getRule() ?? 'python_syntax', category: d.category,
    }));
    for (const site of file.sites ?? []) {
      const matches = nodes.get(`${site.start}:${site.end}`) ?? [];
      const state = { unknown: false };
      let targets: any[] = [];
      let reason = '';
      const errors = site.kind === 'declaration' || site.kind === 'override' ? [] : diagnostics.filter((d: any) => d.category === 0 && d.start < site.end && site.start < d.end);
      // Targets are looked for whatever the diagnostics say: an error in the
      // expression around a name (an Optional concatenated, a call with the
      // wrong arguments) does not change what the name refers to. An error
      // only explains a site that binds nothing.
      if (site.kind === 'override') {
        const name = matches.find((n: any) => n.nodeType === ParseNodeType.Name);
        const fn = name?.parent;
        if (fn?.nodeType === ParseNodeType.Function) {
          let cls = fn.parent;
          while (cls && cls.nodeType !== ParseNodeType.Class && cls.nodeType !== ParseNodeType.Function) cls = cls.parent;
          if (cls?.nodeType === ParseNodeType.Class) {
            const type = evaluator.getTypeOfClass(cls)?.classType;
            for (const base of type?.shared.mro.slice(1) ?? []) {
              if (!isClass(base)) { state.unknown = true; continue; }
              for (const d of base.shared.fields.get(name.d.value)?.getDeclarations() ?? []) {
                if (d.type === DeclarationType.Function) { const t = target(d); if (t) targets.push(t); }
              }
            }
          }
        }
      }
      else if (site.kind === 'call') {
        const call = matches.find((n: any) => n.nodeType === ParseNodeType.Call);
        if (call) {
          const result = evaluator.getTypeOfExpression(call.d.leftExpr);
          state.unknown = !!result.isIncomplete || !!result.typeErrors;
          // Pyright may coalesce same-signature methods when computing the
          // member's union type. Preserve receiver alternatives before that
          // coalescing, otherwise unrelated implementations become one edge.
          const callee = call.d.leftExpr;
          const receiver = callee.nodeType === ParseNodeType.MemberAccess
            ? evaluator.getTypeOfExpression(callee.d.leftExpr).type : undefined;
          if (receiver && isUnion(receiver)) {
            for (const alternative of receiver.priv.subtypes) {
              if (!isClass(alternative)) { state.unknown = true; continue; }
              const member = evaluator.getTypeOfBoundMember(callee, alternative, callee.d.member.d.value);
              if (member) callableTargets(member.type, call, targets, state);
              else state.unknown = true;
            }
          } else callableExpression(callee, call, targets, state);
          if (!targets.length) {
            // A call of something an uninstalled package provides.
            const named = externalPath(callee) ?? (callee.nodeType === ParseNodeType.MemberAccess ? externalBaseMember(callee) : undefined);
            if (named) { targets.push(externalTarget(named)); state.unknown = false; }
          }
        } else reason = 'analyzer_site_mismatch';
      } else {
        const node = matches.find((n: any) => n.nodeType === ParseNodeType.Name);
        if (node) {
          let declarations = evaluator.getDeclInfoForNameNode(node)?.decls ?? [];
          if (site.kind === 'declaration') declarations = declarations.filter((d: any) => {
            const t = target(d); return t && t.path === file.path && t.start <= site.start && t.end >= site.end;
          });
          else declarations = oneVariable(node, declarations);
          targets = declarations.map(target).filter(Boolean);
          if (!targets.length && site.kind !== 'declaration') {
            // A name or member an uninstalled package provides.
            const access = node.parent?.nodeType === ParseNodeType.MemberAccess && node.parent.d.member === node ? node.parent : undefined;
            const named = access ? externalPath(access) ?? externalBaseMember(access) : externalPath(node);
            if (named) targets.push(externalTarget(named));
          }
        } else reason = 'analyzer_site_mismatch';
      }
      targets = oneDefinitionEach([...new Map(targets.map(t => [JSON.stringify(t), t])).values()]);
      const bound = targets.length === 1 && !state.unknown;
      const status = bound ? 'resolved' : targets.length > 1 ? 'ambiguous' : 'unresolved';
      const blamed = !bound && errors.length > 0;
      if (blamed) reason = errors[0].message;
      emit({ event: 'lookup', file: file.id, site: site.id, status, targets,
        unknown: state.unknown, reason: bound ? '' : reason || (state.unknown ? 'unknown_callable' : targets.length ? '' : 'no_declaration'),
        code: blamed ? errors[0].code : undefined, cause: blamed ? 'source_diagnostic' : 'analysis_limitation' });
    }
    emit({ event: 'file_done', file: file.id });
  }
  emit({ event: 'done', context: request.context });
} finally { program.dispose(); service.dispose(); }
