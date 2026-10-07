import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, realpathSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const bridge = fileURLToPath(new URL('../dist/bridge.cjs', import.meta.url));

// analyze writes files under a fresh root and asks for sites, each
// [file, snippet, kind, nth?]; it returns the targets of each site, or
// undefined when the compiler did not bind it.
function analyze(files, sites) { return analyzeWith(files, sites, () => []); }
function analyzeWith(files, sites, packages) {
  const root = realpathSync(mkdtempSync(path.join(tmpdir(), 'ts-bridge-test-')));
  try {
    for (const [name, text] of Object.entries(files)) {
      mkdirSync(path.dirname(path.join(root, name)), { recursive: true });
      writeFileSync(path.join(root, name), text);
    }
    const byFile = new Map();
    sites.forEach(([file, snippet, kind, nth = 0], i) => {
      const text = files[file];
      let at = -1;
      for (let k = 0; k <= nth; k++) at = text.indexOf(snippet, at + 1);
      assert.notEqual(at, -1, `${snippet} not in ${file}`);
      const start = Buffer.byteLength(text.slice(0, at)), end = start + Buffer.byteLength(snippet);
      if (!byFile.has(file)) byFile.set(file, []);
      byFile.get(file).push({ id: String(i), kind, start, end });
    });
    const request = { protocol: 1, context: 'test', root, packages: packages(root), files: Object.keys(files).filter(f => /\.[mc]?[jt]sx?$/.test(f) && !f.includes('node_modules/')).map(f => ({ id: f, path: path.join(root, f), sites: byFile.get(f) ?? [] })) };
    const result = spawnSync(process.execPath, [bridge], { input: JSON.stringify(request), encoding: 'utf8', timeout: 60000, maxBuffer: 64 << 20 });
    assert.equal(result.status, 0, result.stderr);
    const events = result.stdout.trim().split('\n').map(JSON.parse);
    assert.equal(events[0].event, 'hello');
    assert.equal(events[0].typescript, '6.0.3');
    assert.equal(events.at(-1).event, 'done');
    const answers = sites.map(() => undefined);
    for (const e of events) if (e.event === 'lookup') answers[Number(e.site)] = e.targets.map(t => t.path ? { ...t, path: path.relative(root, t.path) } : t);
    return answers;
  } finally { rmSync(root, { recursive: true, force: true }); }
}

const source = (file, name, start) => ({ module: 'source', name, path: file, start, end: start + Buffer.byteLength(name) });
const offset = (text, snippet, nth = 0) => { let at = -1; for (let k = 0; k <= nth; k++) at = text.indexOf(snippet, at + 1); return Buffer.byteLength(text.slice(0, at)); };

test('inferred returns and object members bind across files; the platform is named', () => {
  const a = 'export class Repo {\n  find(id: string) { return { id, title: "x" }; }\n}\nexport function makeRepo() { return new Repo(); }\n';
  const b = 'import { makeRepo } from "./a";\nconst repo = makeRepo();\nconst item = repo.find("1");\nconsole.log(item.title);\n';
  const [make, find, title, log] = analyze({ 'a.ts': a, 'b.ts': b }, [['b.ts', 'makeRepo()', 'call'], ['b.ts', 'repo.find("1")', 'call'], ['b.ts', 'item.title', 'member'], ['b.ts', 'console.log(item.title)', 'call']]);
  assert.deepEqual(make, [source('a.ts', 'makeRepo', offset(a, 'makeRepo'))]);
  assert.deepEqual(find, [source('a.ts', 'find', offset(a, 'find'))]);
  // A member of an object literal names the declarations around it.
  const owner = (name, member) => ({ start: offset(a, name), end: offset(a, name) + name.length, member });
  assert.deepEqual(title, [{ ...source('a.ts', 'title', offset(a, 'title')), owners: [owner('find', 'title'), owner('Repo', 'find.title')] }]);
  assert.deepEqual(log, [{ module: 'lib', name: 'log', qualified: 'Console.log' }]);
});

test('packages are named by package and qualified name; export = is the module itself', () => {
  const files = {
    'node_modules/fakepkg/package.json': '{"name":"fakepkg","types":"index.d.ts"}',
    'node_modules/fakepkg/index.d.ts': 'export interface Reply { data: string }\nexport interface Client { get(path: string): Promise<Reply> }\nexport declare function create(): Client;\n',
    'node_modules/lib2/package.json': '{"name":"lib2"}',
    'node_modules/@types/lib2/package.json': '{"name":"@types/lib2","types":"index.d.ts"}',
    'node_modules/@types/lib2/index.d.ts': 'export = Lib2;\nexport as namespace Lib2;\ndeclare namespace Lib2 { function useThing(): number; }\n',
    'app.ts': 'import { create } from "fakepkg";\nimport { useThing } from "lib2";\nconst c = create();\nc.get("/x").then((r) => r.data);\nuseThing();\n',
  };
  const [create, get, then, data, thing] = analyze(files, [['app.ts', 'create()', 'call'], ['app.ts', 'c.get("/x")', 'call'], ['app.ts', 'c.get("/x").then((r) => r.data)', 'call'], ['app.ts', 'r.data', 'member'], ['app.ts', 'useThing()', 'call']]);
  assert.deepEqual(create, [{ module: 'package', name: 'create', pkg: 'fakepkg', qualified: 'create' }]);
  assert.deepEqual(get, [{ module: 'package', name: 'get', pkg: 'fakepkg', qualified: 'Client.get' }]);
  assert.equal(then[0].module, 'lib');
  assert.equal(then[0].qualified, 'Promise.then');
  assert.deepEqual(data, [{ module: 'package', name: 'data', pkg: 'fakepkg', qualified: 'Reply.data' }]);
  assert.deepEqual(thing, [{ module: 'package', name: 'useThing', pkg: 'lib2', qualified: 'useThing' }]);
});

test('a member of a union binds to each constituent; an override to the base method', () => {
  const text = 'interface A { kind: "a"; size: number }\ninterface B { kind: "b"; size: number }\ndeclare const x: A | B;\nx.size;\nclass Base { run() {} }\nclass Child extends Base { run() {} }\n';
  const [size, override] = analyze({ 'u.ts': text }, [['u.ts', 'x.size', 'member'], ['u.ts', 'run', 'override', 1]]);
  assert.deepEqual(size.map(t => t.start).sort((p, q) => p - q), [offset(text, 'size'), offset(text, 'size', 1)]);
  assert.deepEqual(override, [source('u.ts', 'run', offset(text, 'run'))]);
});

test('constructors, arrow constants, overload implementations and JSX components', () => {
  const text = 'class Svc { constructor(private n: number) {} }\nnew Svc(1);\nconst handler = () => 1;\nhandler();\nfunction f(a: string): void;\nfunction f(a: number): void;\nfunction f(a: any) {}\nf(1);\nconst Button = (p: { title: string }) => null;\nconst el = <Button title="x" />;\n';
  const [ctor, arrow, overload, button] = analyze({ 'c.tsx': text, 'tsconfig.json': '{"compilerOptions":{"jsx":"preserve","strict":true}}' }, [['c.tsx', 'new Svc(1)', 'call'], ['c.tsx', 'handler()', 'call'], ['c.tsx', 'f(1)', 'call'], ['c.tsx', 'Button', 'name', 1]]);
  assert.deepEqual(ctor, [source('c.tsx', 'constructor', offset(text, 'constructor'))]);
  assert.deepEqual(arrow, [source('c.tsx', 'handler', offset(text, 'handler'))]);
  assert.deepEqual(overload, [source('c.tsx', 'f', offset(text, 'function f(a: any)') + 'function '.length)]);
  assert.deepEqual(button, [source('c.tsx', 'Button', offset(text, 'Button'))]);
});

test('path aliases and a solution tsconfig with references', () => {
  const files = {
    'tsconfig.json': '{"files":[],"references":[{"path":"./tsconfig.app.json"}]}',
    'tsconfig.app.json': '{"compilerOptions":{"baseUrl":".","paths":{"@/*":["src/*"]}},"include":["src"]}',
    'src/lib/x.ts': 'export function x() { return 1; }\n',
    'src/main.ts': 'import { x } from "@/lib/x";\nx();\n',
  };
  const [call] = analyze(files, [['src/main.ts', 'x()', 'call']]);
  assert.deepEqual(call, [source('src/lib/x.ts', 'x', offset(files['src/lib/x.ts'], 'function x') + 'function '.length)]);
});

test('byte offsets survive UTF-16 surrogates, CRLF and a byte order mark', () => {
  const text = '﻿const greeting = "😀 héllo";\r\nfunction run() { return greeting; }\r\nrun();\r\n';
  // The first run() is the declaration's; the call is the second.
  const [call] = analyze({ 'e.ts': text }, [['e.ts', 'run()', 'call', 1]]);
  assert.deepEqual(call, [source('e.ts', 'run', offset(text, 'run'))]);
});

test('unbound sites get no answer', () => {
  const text = 'declare const anything: any;\nanything.whatever();\nconst n: string = "";\n';
  const [call, keyword] = analyze({ 'n.ts': text }, [['n.ts', 'anything.whatever()', 'call'], ['n.ts', 'string', 'type']]);
  assert.equal(call, undefined);
  assert.equal(keyword, undefined);
});

test('the states of a discriminated union bind to the member their common base declares', () => {
  const text = 'interface Base { data: unknown; isLoading: boolean }\ninterface Loading extends Base { data: undefined; isLoading: true }\ninterface Done extends Base { data: string; isLoading: false }\ndeclare const q: Loading | Done;\nq.data;\ninterface Input { value: string }\ninterface Area { value: string }\ndeclare const el: Input | Area;\nel.value;\n';
  const [data, value] = analyze({ 'q.ts': text }, [['q.ts', 'q.data', 'member'], ['q.ts', 'el.value', 'member']]);
  assert.deepEqual(data, [source('q.ts', 'data', offset(text, 'data'))]);
  assert.equal(value.length, 2);
});

test('a workspace package exporting uncommitted build output resolves to its sources', () => {
  const files = {
    'packages/common/package.json': '{"name":"@pi/common","exports":{"./audit":{"types":"./dist/audit/index.d.ts","import":"./dist/audit/index.js"},".":{"types":"./dist/index.d.ts"}}}',
    'packages/common/tsconfig.json': '{"compilerOptions":{"rootDir":"src","outDir":"dist","module":"NodeNext","moduleResolution":"NodeNext"},"include":["src/**/*.ts"]}',
    'packages/common/src/audit/index.ts': 'export class AuditStore { save() { return 1; } }\n',
    'packages/common/src/index.ts': 'export function version() { return "1"; }\n',
    'apps/api/tsconfig.json': '{"compilerOptions":{"module":"NodeNext","moduleResolution":"NodeNext"},"include":["src/**/*.ts"]}',
    'apps/api/src/main.ts': 'import { AuditStore } from "@pi/common/audit";\nimport { version } from "@pi/common";\nnew AuditStore().save();\nversion();\n',
    'node_modules/@pi/common': null,
  };
  delete files['node_modules/@pi/common'];
  const audit = files['packages/common/src/audit/index.ts'], index = files['packages/common/src/index.ts'];
  const [save, version] = analyzeWith(files, [['apps/api/src/main.ts', 'new AuditStore().save()', 'call'], ['apps/api/src/main.ts', 'version()', 'call']], root => [{ name: '@pi/common', dir: path.join(root, 'packages/common') }]);
  assert.deepEqual(save, [source('packages/common/src/audit/index.ts', 'save', offset(audit, 'save'))]);
  assert.deepEqual(version, [source('packages/common/src/index.ts', 'version', offset(index, 'version'))]);
});
