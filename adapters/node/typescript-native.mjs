// The native compiler emits private copies; neither source nor project output is written.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import ts from 'typescript';
import MagicString from 'magic-string';
import remapping from '@jridgewell/remapping';
import { collectIdentities } from './typescript-identities.mjs';
import { IDENTITY } from './transform.mjs';

const require = createRequire(import.meta.url);
function nativeBinary() {
  const metadata = require.resolve('@typescript/typescript-' + process.platform + '-' + process.arch + '/package.json');
  if (JSON.parse(fs.readFileSync(metadata, 'utf8')).version !== '7.0.2') throw new Error('native TypeScript platform package version differs from the qualified compiler');
  return path.join(path.dirname(metadata), 'lib', process.platform === 'win32' ? 'tsc.exe' : 'tsc');
}
export function nativeVersion(run = spawnSync) {
  const result = run(nativeBinary(), ['--version'], { encoding: 'utf8', timeout: 30000, maxBuffer: 1048576 });
  if (result.error || result.status !== 0 || result.stdout.trim() !== 'Version 7.0.2') throw new Error('TypeScript native backend requires the pinned executable compiler 7.0.2');
  return '7.0.2';
}
export function nativeEmit(source, filename, sourceName, plan, options, run = spawnSync, root = process.cwd()) {
  const augmented = new MagicString(source);
  const identities = collectIdentities(source, filename, sourceName, plan);
  for (const item of identities) {
    const marker = IDENTITY + Buffer.from(JSON.stringify(item.metadata)).toString('base64');
    // Native lowering can discard constructor/namespace comments. A private string
    // directive survives emission and is removed before probes or application execution.
    augmented.appendLeft(item.position, item.block ? JSON.stringify(marker) + ';' : '/*' + marker + '*/');
  }
  const temporary = fs.realpathSync(os.tmpdir());
  const relative = path.relative(fs.realpathSync(root), temporary);
  if (relative === '' || relative !== '..' && !relative.startsWith('..' + path.sep) && !path.isAbsolute(relative)) throw new Error('native TypeScript workspace must be outside the source project');
  const directory = fs.mkdtempSync(path.join(temporary, 'otelc-native-typescript-'));
  try {
    const input = path.join(directory, path.basename(filename)), output = path.join(directory, 'emitted');
    fs.writeFileSync(input, augmented.toString());
    const settings = Object.fromEntries(ts.serializeCompilerOptions(options));
    delete settings.tsBuildInfoFile;
    Object.assign(settings, { rootDir: directory, outDir: output, noEmit: false, noEmitOnError: false,
      noCheck: true, noResolve: true, incremental: false, composite: false, types: [], moduleResolution: 'bundler' });
    // CommonJS emission uses Node resolution; this adapter does not type-check projects.
    if (settings.module === 'commonjs') Object.assign(settings, { moduleResolution: 'node16', module: 'node16' });
    const config = path.join(directory, 'tsconfig.json');
    fs.writeFileSync(config, JSON.stringify({ compilerOptions: settings, files: [input] }));
    const result = run(nativeBinary(), ['--project', config], { encoding: 'utf8', timeout: 30000, maxBuffer: 1048576 });
    if (result.error || result.status !== 0) throw new Error('TypeScript native emission failed for ' + filename + ': ' + (result.error?.message ?? result.stdout + result.stderr));
    const extension = /\.mts$/.test(filename) ? '.mjs' : /\.cts$/.test(filename) ? '.cjs' : '.js';
    const generated = path.join(output, path.basename(filename).replace(/\.(?:ts|mts|cts)$/, extension));
    const code = fs.readFileSync(generated, 'utf8');
    for (const item of identities) {
      if (!code.includes(IDENTITY + Buffer.from(JSON.stringify(item.metadata)).toString('base64'))) throw new Error('native TypeScript emission lost an original function identity');
    }
    const emitted = JSON.parse(fs.readFileSync(generated + '.map', 'utf8'));
    const original = augmented.generateMap({ source: filename, file: path.basename(input), includeContent: true, hires: true });
    const map = remapping([emitted, original], () => null);
    return { code: code.replace(/\n\/\/# sourceMappingURL=.*(?:\n|$)/, '\n'), map,
      format: /\.cts$/.test(filename) ? 'commonjs' : /\.mts$/.test(filename) ? 'module' : undefined };
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
}
