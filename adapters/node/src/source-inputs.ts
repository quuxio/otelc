import { createHash } from 'node:crypto';
import { closeSync, constants, openSync, readFileSync, realpathSync, writeSync, existsSync } from 'node:fs';
import path from 'node:path';
import type { ICompilerEvidence, ICompilerFile, ISourcePlan } from './source-plan.ts';
import { readStableFile } from './source-file.ts';

interface IRetainedFile {
  path?: string; file: string; sha256: string; size: number;
}

interface IRetainedCompiler extends Omit<ICompilerEvidence, 'files'> {
  files: Record<'input' | 'options' | 'output' | 'map', IRetainedFile> & { preparation_map?: IRetainedFile };
}

interface IModuleLoad {
  readonly id: number;
  readonly path: string;
  readonly logicalSource: string;
  readonly url: string;
}

interface IInput {
  path: string; kind: 'source' | 'compiler_config' | 'absent_config' | 'package_context' | 'absent_package'
    | 'startup' | 'module_requested' | 'compilation' | 'compiled' | 'instrumented' | 'loaded';
  sha256?: string; size?: number;
  format?: string; logical_source?: string; source_sha256?: string; map_sha256?: string; map_size?: number;
  url?: string;
  output_file?: string; map_file?: string;
  requested_path?: string;
  text_sha256?: string;
  load_id?: number;
  module_type?: 'typescript' | 'javascript';
  compiler?: IRetainedCompiler;
  startup?: { pid: number; node: string; cwd: string; argv: string[]; exec_argv: string[];
    environment: Record<string, { state: 'absent' } | { state: 'present'; value: string; sha256: string }> };
}

const digest = (data: string | Uint8Array): string => createHash('sha256').update(data).digest('hex');
const ENVIRONMENT_KEYS = ['NODE_OPTIONS', 'NODE_PATH', 'NODE_PRESERVE_SYMLINKS', 'NODE_EXTRA_CA_CERTS',
  'NODE_ICU_DATA', 'NODE_COMPILE_CACHE', 'NODE_DISABLE_COMPILE_CACHE', 'NODE_COMPILE_CACHE_READONLY',
  'UV_THREADPOOL_SIZE', 'TZ', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TMPDIR', 'TEMP', 'TMP'] as const;

/** Journal the exact original bytes read by our loader/compiler, before transformation. */
export class SourceInputs {
  private readonly descriptor: number;
  private readonly inputs = new Map<string, IInput>();
  private readonly entry: { path: string; sha256: string };
  private readonly directory: string;
  private readonly prefix: string;
  private readonly knownContext: Map<string, { sha256?: string; resolved_path?: string }>;
  private nextLoad = 0;

  constructor(plan: ISourcePlan) {
    const prefix = plan.plain ? 'node-plain' : 'node';
    this.prefix = prefix;
    this.directory = plan.runtime.directory;
    const manifest = JSON.parse(readFileSync(path.join(plan.runtime.directory, prefix + '-launch.json'), 'utf8')) as
      { schema_version: number; launch_id: string; entry: { path: string; sha256: string };
        resolved_plan: ISourcePlan;
        entry_context: { path: string; sha256?: string; resolved_path?: string }[] };
    if (manifest.schema_version !== 4 || manifest.launch_id !== plan.launch_id) throw new Error('source input manifest has an unsupported schema or belongs to another launch');
    if (JSON.stringify(manifest.resolved_plan) !== JSON.stringify(plan)) throw new Error('source plan differs from the retained launch manifest');
    this.entry = manifest.entry;
    this.knownContext = new Map(manifest.entry_context.map(item => [item.path, item]));
    this.descriptor = openSync(path.join(plan.runtime.directory, prefix + '-sources.jsonl'),
      constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
    this.record({ path: this.entry.path, kind: 'startup', startup: { pid: process.pid, node: process.execPath,
      cwd: realpathSync(process.cwd()), argv: [...process.argv], exec_argv: [...process.execArgv],
      environment: Object.fromEntries(ENVIRONMENT_KEYS.map(key => {
        const value = process.env[key];
        return [key, value === undefined ? { state: 'absent' } : { state: 'present', value, sha256: digest(value) }];
      })) } });
    this.packageContext(this.entry.path);
  }

  private record(input: IInput): void {
    const key = input.kind + ':' + (input.load_id ?? input.url ?? input.requested_path ?? input.path);
    const previous = this.inputs.get(key);
    if (previous) {
      if (JSON.stringify(previous) !== JSON.stringify(input)) throw new Error('loaded input changed: ' + input.path);
      return;
    }
    if (input.kind === 'source' && input.path === this.entry.path && input.sha256 !== this.entry.sha256) throw new Error('launch entry changed before load');
    const known = this.knownContext.get(input.requested_path ?? input.path);
    if (known && (known.sha256 !== input.sha256 || known.resolved_path !== undefined && known.resolved_path !== input.path)) throw new Error('entry package context changed before load');
    const bytes = Buffer.from(JSON.stringify(input) + '\n');
    let offset = 0;
    while (offset < bytes.length) offset += writeSync(this.descriptor, bytes, offset, bytes.length - offset);
    this.inputs.set(key, input);
  }

  /** Read once and bind those same bytes; a later filesystem snapshot is insufficient. */
  read(filename: string, kind: 'source' | 'compiler_config' | 'package_context'): string {
    const resolved = realpathSync(filename);
    const bytes = readStableFile(resolved);
    this.record({ path: resolved, requested_path: path.resolve(filename), kind, sha256: digest(bytes),
      text_sha256: digest(bytes.toString('utf8')), size: bytes.length });
    return bytes.toString('utf8');
  }

  /** A preceding loader's altered bytes are an unsupported origin, never the disk source. */
  source(filename: string, loaded?: string): string {
    const original = this.read(filename, 'source');
    if (loaded !== undefined && loaded !== original) throw new Error('a preceding loader changed original source bytes: ' + filename);
    this.packageContext(filename);
    return original;
  }

  private packageContext(filename: string): void {
    let directory = path.dirname(filename);
    while (true) {
      const candidate = path.join(directory, 'package.json');
      if (existsSync(candidate)) { this.read(candidate, 'package_context'); return; }
      this.record({ path: candidate, kind: 'absent_package' });
      const parent = path.dirname(directory);
      if (parent === directory) return;
      directory = parent;
    }
  }

  /** A caught compiler/probe refusal still leaves an incomplete load in the evidence. */
  beginModule(filename: string, logicalSource: string, url: string, moduleType: 'typescript' | 'javascript'): IModuleLoad {
    const identity = Object.freeze({ id: ++this.nextLoad, path: realpathSync(filename), logicalSource, url });
    this.record({ path: identity.path, kind: 'module_requested', load_id: identity.id,
      logical_source: logicalSource, url, module_type: moduleType });
    return identity;
  }

  private compilerFile(file: ICompilerFile): IRetainedFile {
    return { ...(file.path ? { path: file.path } : {}), file: this.retain(file.text),
      sha256: digest(file.text), size: Buffer.byteLength(file.text) };
  }

  /** Preserve actual compiler inputs/options/raw outputs before private workspaces disappear. */
  compilation(load: IModuleLoad, evidence: ICompilerEvidence): void {
    const { files, ...metadata } = evidence;
    this.record({ path: load.path, kind: 'compilation', load_id: load.id,
      compiler: { ...metadata, files: {
        input: this.compilerFile(files.input), options: this.compilerFile(files.options),
        output: this.compilerFile(files.output), map: this.compilerFile(files.map),
        ...(files.preparation_map ? { preparation_map: this.compilerFile(files.preparation_map) } : {}) } } });
  }

  /** Retain the exact compiler/hook output separately from its original file bytes. */
  emitted(load: IModuleLoad, code: string, format: string | undefined,
    kind: 'compiled' | 'instrumented' | 'loaded', map?: object): void {
    const source = this.inputs.get('source:' + load.path);
    if (!source) throw new Error('emitted module has no source input');
    const outputFile = this.retain(code);
    const mapText = map ? JSON.stringify(map) : undefined;
    this.record({ path: load.path, kind, load_id: load.id, sha256: digest(code), size: Buffer.byteLength(code),
      source_sha256: source.sha256, logical_source: load.logicalSource, url: load.url, format: format ?? 'node-detected',
      output_file: outputFile,
      ...(mapText ? { map_sha256: digest(mapText), map_size: Buffer.byteLength(mapText), map_file: this.retain(mapText) } : {}) });
  }

  private retain(content: string): string {
    const filename = path.join(this.directory, this.prefix + '-module-' + digest(content) + '.txt');
    if (existsSync(filename)) {
      if (readFileSync(filename, 'utf8') !== content) throw new Error('retained compiler output changed');
      return filename;
    }
    const descriptor = openSync(filename, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
    try {
      const bytes = Buffer.from(content);
      let offset = 0;
      while (offset < bytes.length) offset += writeSync(descriptor, bytes, offset, bytes.length - offset);
    } finally { closeSync(descriptor); }
    return filename;
  }

  /** TypeScript calls this for the root configuration and every resolved extends read. */
  config = (filename: string): string | undefined => {
    if (!existsSync(filename)) {
      this.record({ path: path.resolve(filename), kind: 'absent_config' });
      return undefined;
    }
    return this.read(filename, 'compiler_config');
  };
}
