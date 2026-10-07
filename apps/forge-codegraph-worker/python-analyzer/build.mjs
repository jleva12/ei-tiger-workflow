import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdir, readFile, writeFile, cp, rm } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { build } from 'esbuild';

const root = path.dirname(fileURLToPath(import.meta.url));
const commit = '7f78dd6e06ae7fab6f0c740b85b7436b2738316d';
const digest = '3bef0ce65b99a8376c5af57f58f00e0c437e9700c6dad9547f611099a2281694';
const cache = path.join(root, '.cache');
await mkdir(cache, { recursive: true });
const archive = path.join(cache, 'pyright.tar.gz');
if (!existsSync(archive)) {
  const response = await fetch(`https://codeload.github.com/microsoft/pyright/tar.gz/${commit}`);
  if (!response.ok) throw new Error(`Pyright download: ${response.status}`);
  await writeFile(archive, Buffer.from(await response.arrayBuffer()));
}
if (createHash('sha256').update(await readFile(archive)).digest('hex') !== digest) {
  throw new Error('Pyright archive integrity mismatch');
}
const upstream = path.join(cache, `pyright-${commit}`, 'packages/pyright-internal');
// Recreate extracted source from verified bytes; a modified cache must not
// change what the version pin means. Also remove obsolete stubs on upgrades.
await rm(path.join(cache, `pyright-${commit}`), { recursive: true, force: true });
execFileSync('tar', ['-xzf', archive, '-C', cache]);
await rm(path.join(root, 'dist'), { recursive: true, force: true });
await mkdir(path.join(root, 'dist'), { recursive: true });
await build({
  absWorkingDir: root, entryPoints: ['src/bridge.ts'], outfile: 'dist/bridge.cjs',
  bundle: true, platform: 'node', format: 'cjs', target: 'node22',
  nodePaths: [path.join(root, 'node_modules')],
  plugins: [{ name: 'pinned-pyright', setup(b) {
    b.onResolve({ filter: /^pyright\// }, args => ({ path: path.join(upstream, 'src', args.path.slice(8) + '.ts') }));
  }}],
});
await cp(path.join(upstream, 'typeshed-fallback'), path.join(root, 'dist/typeshed-fallback'), { recursive: true });
await cp(path.join(cache, `pyright-${commit}`, 'LICENSE.txt'), path.join(root, 'dist/PYRIGHT-LICENSE.txt'));
await writeFile(path.join(root, 'dist/version.json'), JSON.stringify({ protocol: 1, pyright: '1.1.414', commit, bridge: '1' }) + '\n');
