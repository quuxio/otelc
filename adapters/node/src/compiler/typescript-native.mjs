// The native compiler emits private copies; neither source nor project output is written.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import ts from 'typescript-api';
import MagicString from 'magic-string';
import remapping from '@jridgewell/remapping';
import { collectIdentities } from './typescript-identities.mjs';
import { IDENTITY } from './transform.mjs';
import { collectLifetimeClasses, TYPE_IDENTITY } from './typescript-lifetimes.mjs';
import { readStableFile } from '../source-file.ts';

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
function prepareSource(source, filename, sourceName, plan) {
  const augmented = new MagicString(source);
  const identities = plan.plain ? [] : collectIdentities(source, filename, sourceName, plan);
  const classes = plan.plain ? [] : collectLifetimeClasses(source, filename, sourceName, plan);
  for (const item of classes) {
    const marker = JSON.stringify(TYPE_IDENTITY + Buffer.from(JSON.stringify(item.metadata)).toString('base64')) + ';';
    augmented.appendLeft(item.position, item.implicit ? 'constructor(){' + marker + '}' : marker);
  }
  for (const item of identities) {
    const marker = IDENTITY + Buffer.from(JSON.stringify(item.metadata)).toString('base64');
    // Native lowering can discard constructor/namespace comments. A private string
    // directive survives emission and is removed before probes or application execution.
    augmented.appendLeft(item.position, item.block ? JSON.stringify(marker) + ';' : '/*' + marker + '*/');
  }
  return { augmented, identities, classes };
}

function writeCompilerConfig(directory, input, output, options) {
  const settings = Object.fromEntries(ts.serializeCompilerOptions(options));
  delete settings.tsBuildInfoFile;
  Object.assign(settings, { rootDir: directory, outDir: output, noEmit: false, noEmitOnError: false,
    noCheck: true, noResolve: true, incremental: false, composite: false, types: [], moduleResolution: 'bundler' });
  // CommonJS emission uses Node resolution; this adapter does not type-check projects.
  if (settings.module === 'commonjs') Object.assign(settings, { moduleResolution: 'node16', module: 'node16' });
  const config = path.join(directory, 'tsconfig.json');
  fs.writeFileSync(config, JSON.stringify({ compilerOptions: settings, files: [input] }), { mode: 0o600, flag: 'wx' });
  return config;
}

function emittedFormat(filename) {
  if (filename.endsWith('.cts')) return 'commonjs';
  if (filename.endsWith('.mts')) return 'module';
  return undefined;
}

export function nativeEmit(source, filename, sourceName, plan, options, run = spawnSync, root = process.cwd()) {
  const { augmented, identities, classes } = prepareSource(source, filename, sourceName, plan);
  const temporary = fs.realpathSync(os.tmpdir());
  const relative = path.relative(fs.realpathSync(root), temporary);
  if (relative === '' || relative !== '..' && !relative.startsWith('..' + path.sep) && !path.isAbsolute(relative)) throw new Error('native TypeScript workspace must be outside the source project');
  const directory = fs.mkdtempSync(path.join(temporary, 'otelc-native-typescript-'));
  try {
    // Node16 emission consults package type for .ts. A private .cts fixes the
    // requested CJS format even when an ancestor/private package says module.
    const inputName = options.module === ts.ModuleKind.CommonJS
      ? path.basename(filename).replace(/\.ts$/, '.cts') : path.basename(filename);
    const input = path.join(directory, inputName), output = path.join(directory, 'emitted');
    fs.writeFileSync(input, augmented.toString(), { mode: 0o600, flag: 'wx' });
    const config = writeCompilerConfig(directory, input, output, options);
    const inputBytes = readStableFile(input), configBytes = readStableFile(config);
    const binary = fs.realpathSync(nativeBinary()), argv = [binary, '--project', config];
    const result = run(binary, argv.slice(1), { encoding: 'utf8', timeout: 30000, maxBuffer: 1048576 });
    if (!readStableFile(input).equals(inputBytes) || !readStableFile(config).equals(configBytes)) throw new Error('native compiler input or configuration changed during emission');
    if (result.error || result.status !== 0) throw new Error('TypeScript native emission failed for ' + filename + ': ' + (result.error?.message ?? result.stdout + result.stderr));
    const format = emittedFormat(input);
    let extension = '.js';
    if (format === 'module') extension = '.mjs';
    if (format === 'commonjs') extension = '.cjs';
    const generated = path.join(output, path.basename(input).replace(/\.(?:ts|mts|cts)$/, extension));
    const code = readStableFile(generated).toString('utf8');
    for (const item of classes) {
      if (!code.includes(TYPE_IDENTITY + Buffer.from(JSON.stringify(item.metadata)).toString('base64'))) throw new Error('native TypeScript emission lost an original class identity');
    }
    for (const item of identities) {
      if (!code.includes(IDENTITY + Buffer.from(JSON.stringify(item.metadata)).toString('base64'))) throw new Error('native TypeScript emission lost an original function identity');
    }
    const rawMap = readStableFile(generated + '.map').toString('utf8');
    const emitted = JSON.parse(rawMap);
    const original = augmented.generateMap({ source: filename, file: path.basename(input), includeContent: true, hires: true });
    const map = remapping([emitted, original], () => null);
    return { code: code.replace(/\n\/\/# sourceMappingURL=.*(?:\n|$)/, '\n'), map, format,
      compilation: { backend: 'native', version: '7.0.2', compiler_path: binary, cwd: process.cwd(), argv,
        transformer_profile: plan.plain ? 'none' : 'original-identities-v1',
        files: { input: { path: input, text: inputBytes.toString('utf8') },
          options: { path: config, text: configBytes.toString('utf8') },
          output: { path: generated, text: code }, map: { path: generated + '.map', text: rawMap },
          preparation_map: { text: JSON.stringify(original) } } } };
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
}
