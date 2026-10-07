import { cp, mkdir, readdir, readFile, rm, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { build } from 'esbuild';

// The bridge bundles the pinned TypeScript compiler, whose standard library
// declarations (lib.*.d.ts) are read at run time from dist/lib.
const version = '6.0.3';
const root = path.dirname(fileURLToPath(import.meta.url));
const compiler = path.join(root, 'node_modules/typescript');
const installed = JSON.parse(await readFile(path.join(compiler, 'package.json'), 'utf8')).version;
if (installed !== version) throw new Error(`TypeScript ${installed} is installed; the bridge pins ${version} (run npm ci)`);
const dist = path.join(root, 'dist');
await rm(dist, { recursive: true, force: true });
await mkdir(path.join(dist, 'lib'), { recursive: true });
await build({
  absWorkingDir: root, entryPoints: ['src/bridge.ts'], outfile: 'dist/bridge.cjs',
  bundle: true, platform: 'node', format: 'cjs', target: 'node20', logLevel: 'warning',
});
for (const name of await readdir(path.join(compiler, 'lib'))) {
  if (/^lib\..*\.d\.ts$/.test(name)) await cp(path.join(compiler, 'lib', name), path.join(dist, 'lib', name));
}
await cp(path.join(compiler, 'LICENSE.txt'), path.join(dist, 'TYPESCRIPT-LICENSE.txt'));
await writeFile(path.join(dist, 'version.json'), JSON.stringify({ protocol: 1, typescript: version, bridge: '1' }) + '\n');
