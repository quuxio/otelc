import { spawn } from 'node:child_process';
import type { ChildProcess } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { closeSync, constants, openSync, readFileSync, fstatSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defaultResource, resourceFromAttributes } from '@opentelemetry/resources';
import { AlwaysOffSampler, ParentBasedSampler, TraceIdRatioBasedSampler, TracerProvider } from '@opentelemetry/sdk-trace';
import type { SpanProcessor } from '@opentelemetry/sdk-trace';
import type { MeterOptions } from '@opentelemetry/api';
import { spanToWire } from './export-wire.ts';
import type { IControlDescriptor } from './local-metric-control.ts';
import { MetricControls } from './metric-controls.ts';
import { SourceHandoffWriter } from './source-handoff.ts';
import { SourceMetrics } from './source-metrics.ts';
import type { ISourcePlan } from './source-plan.ts';
import type { ISourceSdkReply, SourceCheckpointType, SourceSdkRecordType } from './source-sdk-protocol.ts';

interface IPending { resolve: (status: object) => void; reject: (error: Error) => void; timer: NodeJS.Timeout }

class SourceControls extends MetricControls {
  readonly #changed: () => void;
  constructor(plan: ISourcePlan, changed: () => void) {
    super(plan.metrics.collection, plan.metrics.emission); this.#changed = changed;
  }
  override setCollection(value: boolean): void { super.setCollection(value); this.#changed(); }
  override setEmission(value: boolean): void { super.setEmission(value); this.#changed(); }
}

function randomId(size: number): string {
  const id = randomBytes(size).toString('hex');
  return /^0+$/.test(id) ? '0'.repeat(size * 2 - 1) + '1' : id;
}

function readReadiness(filename: string): Record<string, unknown> | undefined {
  let descriptor: number;
  try { descriptor = openSync(filename, constants.O_RDONLY | constants.O_NOFOLLOW); }
  catch (error) {
    if (error instanceof Error && 'code' in error && error.code === 'ENOENT') return;
    throw error;
  }
  try {
    const metadata = fstatSync(descriptor);
    if (!metadata.isFile() || metadata.uid !== process.getuid?.() || (metadata.mode & 0o7777) !== 0o600
        || metadata.size > 4096) throw new Error('source SDK readiness is not owned private metadata');
    return JSON.parse(readFileSync(descriptor, 'utf8')) as Record<string, unknown>;
  } finally { closeSync(descriptor); }
}

function matchesReadyOwner(ready: Record<string, unknown>, plan: ISourcePlan, pid: number): boolean {
  return Object.keys(ready).length === 3 && Object.hasOwn(ready, 'schema_version')
    && Object.hasOwn(ready, 'pid') && Object.hasOwn(ready, 'launch_id')
    && ready.schema_version === 1 && ready.pid === pid && ready.launch_id === plan.launch_id;
}

function waitForReady(plan: ISourcePlan, pid: number): void {
  const deadline = performance.now() + 10000;
  const pause = new Int32Array(new SharedArrayBuffer(4));
  const filename = path.join(plan.runtime.directory, 'node-sdk-ready.json');
  while (true) {
    const ready = readReadiness(filename);
    if (ready !== undefined) {
      if (!matchesReadyOwner(ready, plan, pid)) throw new Error('source SDK readiness identity does not match its owner');
      return;
    }
    if (performance.now() >= deadline) throw new Error('source SDK startup timed out');
    Atomics.wait(pause, 0, 0, 1);
  }
}

/** The application owns scopes/probes; an independent child owns SDK aggregation and drain. */
export class SourceSdk {
  readonly controls: MetricControls;
  readonly tracerProvider: TracerProvider;
  readonly #plan: ISourcePlan;
  readonly #writer: SourceHandoffWriter;
  readonly #child: ChildProcess;
  readonly #pending = new Map<number, IPending>();
  #id = 1;
  #failed = false;
  #stopped = false;
  #unavailableLosses = 0;
  #sampledStarted = 0;
  #sampledEnded = 0;
  #spansHandedOff = 0;
  #closing: Promise<void> | undefined;
  onCollectionChange?: () => void;

  private constructor(plan: ISourcePlan, writer: SourceHandoffWriter, child: ChildProcess) {
    this.#plan = plan; this.#writer = writer; this.#child = child;
    this.controls = new SourceControls(plan, () => {
      if (!this.send({ kind: 'controls', collection: this.controls.collection, emission: this.controls.emission })) {
        throw new Error('source SDK control handoff failed');
      }
      this.onCollectionChange?.();
    });
    const resource = defaultResource().merge(resourceFromAttributes({ 'service.name': plan.resource.service_name,
      'service.instance.id': plan.launch_id, 'otelc.language': plan.language }));
    const processor: SpanProcessor = {
      onStart: () => { this.#sampledStarted++; },
      onEnd: span => {
        this.#sampledEnded++;
        if (this.send({ kind: 'span', span: spanToWire(span) })) this.#spansHandedOff++;
      },
      forceFlush: () => Promise.resolve(), shutdown: () => Promise.resolve(),
    };
    this.tracerProvider = new TracerProvider({ resource,
      sampler: plan.traces.enabled ? new ParentBasedSampler({ root: new TraceIdRatioBasedSampler(plan.traces.sample_ratio) })
        : new AlwaysOffSampler(),
      idGenerator: { generateTraceId: () => randomId(16), generateSpanId: () => randomId(8) }, spanProcessors: [processor] });
    child.on('message', message => {
      const reply = message as ISourceSdkReply;
      const pending = this.#pending.get(reply.id);
      if (!pending) return;
      this.#pending.delete(reply.id); clearTimeout(pending.timer);
      if (reply.ok) pending.resolve(reply.status);
      else pending.reject(new Error('source SDK operation failed'));
    });
    child.on('error', () => this.#fail());
    child.on('exit', () => this.#fail());
  }

  /** Wait before the application's first event-loop turn, without running its timers. */
  static start(plan: ISourcePlan): SourceSdk {
    const filename = path.join(plan.runtime.directory, 'node-events.fifo');
    const reader = openSync(filename, constants.O_RDONLY | constants.O_NONBLOCK | constants.O_NOFOLLOW);
    let writer: SourceHandoffWriter | undefined;
    let child: ChildProcess | undefined;
    let diagnostic: number | undefined;
    let preparedPlan: number | undefined;
    try {
      diagnostic = openSync(path.join(plan.runtime.directory, 'node-sdk.log'), 'wx', 0o600);
      writer = new SourceHandoffWriter(filename);
      const environment = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
        key !== 'NODE_OPTIONS' && !key.startsWith('OTEL_') && !key.startsWith('OTELC_')));
      preparedPlan = openSync(path.join(plan.runtime.directory, 'node-plan.json'), constants.O_RDONLY | constants.O_NOFOLLOW);
      const metadata = fstatSync(preparedPlan);
      if (!metadata.isFile() || metadata.uid !== process.getuid?.() || (metadata.mode & 0o7777) !== 0o600) {
        throw new Error('source SDK plan is not owned private metadata');
      }
      child = spawn(process.execPath, [fileURLToPath(new URL('./source-sdk-child.ts', import.meta.url))], {
        env: environment, stdio: ['ignore', 'ignore', diagnostic, 'ipc', preparedPlan] });
      // A spawn failure can emit after synchronous startup has already refused it.
      child.on('error', () => { /* The checked startup failure belongs to the caller. */ });
      if (!child.pid) throw new Error('source SDK startup could not create its owner');
      waitForReady(plan, child.pid);
      const sdk = new SourceSdk(plan, writer, child);
      child.channel?.unref(); child.unref();
      return sdk;
    } catch (error) { writer?.close(); child?.kill(); throw error; }
    finally {
      closeSync(reader);
      if (diagnostic !== undefined) closeSync(diagnostic);
      if (preparedPlan !== undefined) closeSync(preparedPlan);
    }
  }

  send(record: SourceSdkRecordType): boolean {
    if (this.#failed) { this.#unavailableLosses++; return false; }
    return this.#writer.send(record);
  }

  metrics(scope: string, version = '0.2.0', options?: MeterOptions): SourceMetrics {
    return new SourceMetrics(scope, this.controls, record => this.send(record), version, options);
  }
  descriptor(value: IControlDescriptor): void {
    if (!this.send({ kind: 'control-descriptor', descriptor: value })) throw new Error('control ownership handoff failed');
  }
  checkpoint(status: SourceCheckpointType): void { this.send({ kind: 'checkpoint', status }); }
  handoffStatus() {
    const counts = this.#writer.status();
    return { ...counts, attempted: counts.attempted + this.#unavailableLosses,
      lost: counts.lost + this.#unavailableLosses, sdk_process_failed: this.#failed };
  }
  spanObservation() {
    return { sampled_started: this.#sampledStarted, sampled_ended: this.#sampledEnded,
      sampled_active: this.#sampledStarted - this.#sampledEnded, handed_off: this.#spansHandedOff };
  }

  #fail(): void {
    this.#writer.close();
    if (this.#stopped) return;
    this.#failed = true;
    for (const pending of this.#pending.values()) { clearTimeout(pending.timer); pending.reject(new Error('source SDK process exited')); }
    this.#pending.clear();
  }

  #request(command: 'status' | 'flush' | 'shutdown'): Promise<object> {
    if (this.#pending.size >= 32) return Promise.reject(new Error('too many source SDK requests'));
    const id = this.#id++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.#pending.delete(id); reject(new Error('source SDK request timed out'));
      }, this.#plan.export.shutdown_timeout_ms).unref();
      this.#pending.set(id, { resolve, reject, timer });
      if (!this.send({ kind: 'request', id, command })) {
        clearTimeout(timer); this.#pending.delete(id); reject(new Error('source SDK handoff failed'));
      }
    });
  }

  status(): Promise<object> { return this.#request('status'); }
  async flush(): Promise<void> { await this.#request('flush'); }
  shutdown(): Promise<void> {
    if (this.#closing) return this.#closing;
    // Explicit close/startup refusal must settle before its caller continues.
    // Natural application exit never calls this path: FIFO EOF owns that drain.
    this.#child.channel?.ref();
    this.#closing = this.#request('shutdown').then(() => { this.#stopped = true; }).finally(() => {
      this.#writer.close(); this.#child.channel?.unref();
    });
    return this.#closing;
  }
}
