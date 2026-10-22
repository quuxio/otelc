import { createHash } from 'node:crypto';
import { closeSync, constants, fstatSync, openSync, readFileSync, realpathSync, statSync, writeSync, existsSync } from 'node:fs';
import path from 'node:path';
import type { ISourcePlan } from './source-plan.ts';

interface IInput {
  path: string; kind: 'source' | 'compiler_config' | 'absent_config' | 'package_context' | 'absent_package'
    | 'compiled' | 'instrumented' | 'loaded';
  sha256?: string; size?: number;
  format?: string; logical_source?: string; source_sha256?: string; map_sha256?: string;
  url?: string;
  output_file?: string; map_file?: string;
  requested_path?: string;
}

const digest = (data: string | Uint8Array): string => createHash('sha256').update(data).digest('hex');

/** Journal the exact original bytes read by our loader/compiler, before transformation. */
export class SourceInputs {
  private readonly descriptor: number;
  private readonly inputs = new Map<string, IInput>();
  private readonly entry: { path: string; sha256: string };
  private readonly directory: string;
  private readonly prefix: string;
  private readonly knownContext: Map<string, { sha256?: string; resolved_path?: string }>;

  constructor(plan: ISourcePlan) {
    const prefix = plan.plain ? 'node-plain' : 'node';
    this.prefix = prefix;
    this.directory = plan.runtime.directory;
    const manifest = JSON.parse(readFileSync(path.join(plan.runtime.directory, prefix + '-launch.json'), 'utf8')) as
      { launch_id: string; entry: { path: string; sha256: string };
        entry_context: { path: string; sha256?: string; resolved_path?: string }[] };
    if (manifest.launch_id !== plan.launch_id) throw new Error('source input manifest belongs to another launch');
    this.entry = manifest.entry;
    this.knownContext = new Map(manifest.entry_context.map(item => [item.path, item]));
    this.descriptor = openSync(path.join(plan.runtime.directory, prefix + '-sources.jsonl'),
      constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
    this.packageContext(this.entry.path);
  }

  private record(input: IInput): void {
    const key = input.kind + ':' + (input.url ?? input.requested_path ?? input.path);
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
    const descriptor = openSync(resolved, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    let bytes: Buffer;
    try {
      const before = fstatSync(descriptor, { bigint: true });
      if (!before.isFile()) throw new Error('loaded input is not a regular file');
      bytes = readFileSync(descriptor);
      const after = fstatSync(descriptor, { bigint: true });
      const current = statSync(resolved, { bigint: true });
      const stable = (value: typeof before): string => [value.dev, value.ino, value.size, value.mtimeNs, value.ctimeNs].join(':');
      if (stable(before) !== stable(after) || stable(after) !== stable(current)) throw new Error('loaded input changed during read');
    } finally {
      // This descriptor is only the input read; the journal stays open until process exit.
      closeSync(descriptor);
    }
    this.record({ path: resolved, requested_path: path.resolve(filename), kind, sha256: digest(bytes), size: bytes.length });
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

  /** Retain the exact compiler/hook output separately from its original file bytes. */
  emitted(filename: string, logicalSource: string, url: string, code: string, format: string | undefined,
    kind: 'compiled' | 'instrumented' | 'loaded', map?: object): void {
    const source = this.inputs.get('source:' + realpathSync(filename));
    if (!source) throw new Error('emitted module has no source input');
    const outputFile = this.retain(code);
    this.record({ path: realpathSync(filename), kind, sha256: digest(code), size: Buffer.byteLength(code),
      source_sha256: source.sha256, logical_source: logicalSource, url, format: format ?? 'node-detected',
      output_file: outputFile,
      ...(map ? { map_sha256: digest(JSON.stringify(map)), map_file: this.retain(JSON.stringify(map)) } : {}) });
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
