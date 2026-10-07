import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';

const bridge = fileURLToPath(new URL('../dist/bridge.cjs', import.meta.url));
function analyze(source, calls) {
  const root = mkdtempSync(path.join(tmpdir(), 'python-bridge-test-'));
  try {
    const p = path.join(root, 'app.py'); writeFileSync(p, source);
    const sites = calls.map((call, i) => {
      const start = source.indexOf(call);
      assert.notEqual(start, -1);
      return { id: String(i), kind: 'call', start: Buffer.byteLength(source.slice(0, start)), end: Buffer.byteLength(source.slice(0, start + call.length)) };
    });
    const input = { protocol: 1, context: 'test', root, version: '3.12', platform: 'Linux', roots: [], dependencies: [],
      files: [{ id: 'app', path: p, sha256: createHash('sha256').update(source).digest('hex'), sites }] };
    const result = spawnSync(process.execPath, [bridge], { input: JSON.stringify(input), encoding: 'utf8', timeout: 15000, maxBuffer: 4 << 20 });
    assert.equal(result.status, 0, result.stderr);
    const events = result.stdout.trim().split('\n').map(JSON.parse);
    assert.equal(events[0].pyright, '1.1.414');
    assert.equal(events.at(-1).event, 'done');
    return events.filter(e => e.event === 'lookup');
  } finally { rmSync(root, { recursive: true, force: true }); }
}

test('empty module completes', () => assert.deepEqual(analyze('', []), []));
test('identical callable signatures retain both identities', () => {
  const [call] = analyze('def a() -> int: return 1\ndef b() -> int: return 2\ndef use(flag: bool):\n    fn = a if flag else b\n    return fn()\n', ['fn()']);
  assert.equal(call.status, 'ambiguous');
  assert.deepEqual(call.targets.map(t => t.name).sort(), ['a', 'b']);
});
test('UTF-16 analyzer offsets map back to UTF-8 bytes', () => {
  const source = 'message = "😀"\r\ndef run() -> int: return 1\r\nvalue = run()\r\n';
  const [call] = analyze(source, ['run()\r\n']);
  // Query exactly the call; the extra newline deliberately cannot match.
  assert.equal(call.reason, 'analyzer_site_mismatch');
  const [exact] = analyze(source, ['run()']);
  // The first spelling is in the definition, which is not a CallNode.
  assert.equal(exact.reason, 'analyzer_site_mismatch');
  const [actual] = analyze('message = "😀"\r\ndef run(): return 1\r\nrun( )\r\n', ['run( )']);
  assert.equal(actual.status, 'resolved');
  assert.equal(actual.targets[0].start, Buffer.byteLength('message = "😀"\r\ndef '));
});
test('Any in receiver union prevents an exact target', () => {
  const [call] = analyze('from typing import Any\nclass A:\n    def run(self): pass\ndef use(a: A | Any):\n    a.run()\n', ['a.run()']);
  assert.equal(call.status, 'unresolved'); assert.equal(call.unknown, true);
});
