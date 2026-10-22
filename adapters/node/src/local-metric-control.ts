import { createHash, randomBytes, timingSafeEqual } from 'node:crypto';
import { closeSync, fchmodSync, fstatSync, linkSync, lstatSync, openSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import type { AddressInfo, Server, Socket } from 'node:net';
import path from 'node:path';
import type { MetricControls } from './metric-controls.ts';

interface IDescriptorIdentity { device: bigint; inode: bigint; sha256?: string }
export interface IControlDescriptor { filename: string; device: string; inode: string; sha256: string }
export interface IControlledPipeline {
  readonly controls: MetricControls;
  status(): object | Promise<object>;
  flush(): Promise<void>;
}

function digest(data: Buffer | string): string { return createHash('sha256').update(data).digest('hex'); }

function removeOwnedFile(filename: string, identity: IDescriptorIdentity): void {
  try {
    const current = lstatSync(filename, { bigint: true });
    if (current.isFile() && current.dev === identity.device && current.ino === identity.inode
        && (identity.sha256 === undefined || digest(readFileSync(filename)) === identity.sha256)) unlinkSync(filename);
  } catch (error) {
    if (!(error instanceof Error) || !('code' in error) || error.code !== 'ENOENT') throw error;
  }
}

/** A surviving SDK owner removes only the unchanged descriptor published by its parent. */
export function removeControlDescriptor(descriptor: IControlDescriptor): void {
  removeOwnedFile(descriptor.filename, { device: BigInt(descriptor.device), inode: BigInt(descriptor.inode),
    sha256: descriptor.sha256 });
}

function publishDescriptor(filename: string, contents: string): IDescriptorIdentity {
  const temporary = filename + '.' + randomBytes(16).toString('hex');
  const file = openSync(temporary, 'wx', 0o600);
  const metadata = fstatSync(file, { bigint: true });
  const identity = { device: metadata.dev, inode: metadata.ino, sha256: digest(contents) };
  try {
    fchmodSync(file, 0o600);
    writeFileSync(file, contents);
    // Publish complete bytes atomically and refuse to overwrite an existing endpoint.
    linkSync(temporary, filename);
    return identity;
  } finally {
    closeSync(file);
    removeOwnedFile(temporary, { device: identity.device, inode: identity.inode });
  }
}

function commandFrom(data: Buffer, secret: Buffer): string | undefined {
  try {
    const value: unknown = JSON.parse(data.toString('utf8'));
    if (!value || typeof value !== 'object' || Array.isArray(value)) return;
    const fields = value as Record<string, unknown>;
    if (Object.keys(fields).length !== 2 || typeof fields.token !== 'string'
        || !/^[a-f0-9]{64}$/.test(fields.token) || typeof fields.command !== 'string') return;
    if (!timingSafeEqual(Buffer.from(fields.token, 'ascii'), secret)) return;
    return fields.command;
  } catch { return; }
}

/** Private authenticated control transport shared by JS and TS owned pipelines. */
export class LocalMetricControl {
  readonly filename: string;
  readonly #pipeline: IControlledPipeline;
  readonly #server: Server;
  readonly #secret = randomBytes(32).toString('hex');
  readonly #connections = new Set<Socket>();
  #identity: IDescriptorIdentity | undefined;
  #closed = false;
  #transportFailed = false;
  #closing: Promise<void> | undefined;

  private constructor(directory: string, pipeline: IControlledPipeline) {
    this.filename = path.join(directory, 'control.json');
    this.#pipeline = pipeline;
    this.#server = createServer(socket => this.#accept(socket));
    this.#server.maxConnections = 32;
    // Transport errors are exposed to the owner; they must not throw into app code.
    this.#server.on('error', () => { this.#transportFailed = true; });
  }

  get transportFailed(): boolean { return this.#transportFailed; }

  /** Publish only after listening; the endpoint file never contains partial JSON. */
  static async listen(directory: string, pipeline: IControlledPipeline): Promise<LocalMetricControl> {
    const metadata = lstatSync(directory);
    if (!metadata.isDirectory() || metadata.uid !== process.getuid?.()
        || (metadata.mode & 0o7777) !== 0o700) throw new Error('control directory must be owned and mode 0700');
    const control = new LocalMetricControl(directory, pipeline);
    try {
      await new Promise<void>((resolve, reject) => {
        control.#server.once('error', reject);
        control.#server.listen({ port: 0, host: '127.0.0.1', exclusive: true }, () => {
          control.#server.removeListener('error', reject);
          resolve();
        });
      });
      // Only this class owns the server; the completed TCP listen supplies AddressInfo.
      const address = control.#server.address() as AddressInfo;
      const contents = JSON.stringify({ schema_version: 1, kind: 'otelc.control.tcp', host: '127.0.0.1',
        port: address.port, pid: process.pid, token: control.#secret }) + '\n';
      control.#identity = publishDescriptor(control.filename, contents);
      control.#server.unref();
      return control;
    } catch (error) { await control.close(); throw error; }
  }

  /** Share identity only, never the control authentication key, with the private SDK process. */
  cleanupDescriptor(): IControlDescriptor {
    if (!this.#identity?.sha256) throw new Error('control endpoint is not published');
    return { filename: this.filename, device: String(this.#identity.device), inode: String(this.#identity.inode),
      sha256: this.#identity.sha256 };
  }

  #accept(socket: Socket): void {
    if (this.#closed) { socket.destroy(); return; }
    this.#connections.add(socket);
    socket.once('close', () => this.#connections.delete(socket));
    socket.on('error', () => { /* A control peer disconnect must not replace application behaviour. */ });
    socket.unref();
    let data = Buffer.alloc(0);
    let completed = false;
    socket.setTimeout(200, () => socket.destroy());
    socket.on('data', chunk => {
      if (completed) return;
      if (data.length + chunk.length > 1024) { completed = true; socket.destroy(); return; }
      data = Buffer.concat([data, chunk]);
      const newline = data.indexOf(10);
      if (newline < 0) return;
      completed = true;
      socket.setTimeout(0);
      const command = newline === data.length - 1 ? commandFrom(data.subarray(0, newline), Buffer.from(this.#secret, 'ascii')) : undefined;
      void this.#execute(command).catch(() => ({ error: 'owned SDK control failed' }))
        .then(reply => socket.end(JSON.stringify(reply) + '\n'));
    });
  }

  async #execute(command: string | undefined): Promise<object> {
    switch (command) {
      case 'on': this.#pipeline.controls.setCollection(true); this.#pipeline.controls.setEmission(true); break;
      case 'off': this.#pipeline.controls.setCollection(false); this.#pipeline.controls.setEmission(false); break;
      case 'collection on': this.#pipeline.controls.setCollection(true); break;
      case 'collection off': this.#pipeline.controls.setCollection(false); break;
      case 'emission on': this.#pipeline.controls.setEmission(true); break;
      case 'emission off': this.#pipeline.controls.setEmission(false); break;
      case 'status': break;
      case 'flush':
        try { await this.#pipeline.flush(); }
        catch { return { error: 'owned SDK flush failed' }; }
        break;
      default: return { error: 'invalid control request' };
    }
    try { return await this.#pipeline.status(); }
    catch { return { error: 'owned SDK status failed' }; }
  }

  /** Destroy idle peers before closing; preserve a replaced or edited descriptor. */
  close(): Promise<void> {
    this.#closed = true;
    this.#closing ??= new Promise<void>((resolve, reject) => {
      const finish = (error?: Error): void => {
        try {
          if (this.#identity) removeOwnedFile(this.filename, this.#identity);
          if (error) reject(error);
          else resolve();
        } catch (error_) { reject(error_); }
      };
      if (this.#server.listening) this.#server.close(finish);
      else finish();
      for (const connection of this.#connections) connection.destroy();
    });
    return this.#closing;
  }
}
