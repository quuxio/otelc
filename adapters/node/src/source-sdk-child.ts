import { constants, createReadStream, fstatSync, openSync, closeSync, ftruncateSync, readFileSync, writeSync } from 'node:fs';
import path from 'node:path';
import { ROOT_CONTEXT, trace } from '@opentelemetry/api';
import type { Counter, Gauge, Histogram } from '@opentelemetry/api';
import { contextFromWire, spanFromWire } from './export-wire.ts';
import { publishDescriptor, removeControlDescriptor } from './local-metric-control.ts';
import type { IControlDescriptor } from './local-metric-control.ts';
import { OwnedSdkPipeline } from './sdk-pipeline.ts';
import { SourceHandoffReader } from './source-handoff.ts';
import type { ISourcePlan } from './source-plan.ts';
import type { ISourceMetric, SourceCheckpointType, SourceSdkRecordType } from './source-sdk-protocol.ts';

function inheritedPlan(): ISourcePlan {
  // FD 4 is the parent's verified private plan, not an application/CLI pathname.
  const descriptor = 4;
  const metadata = fstatSync(descriptor);
  if (!metadata.isFile() || metadata.uid !== process.getuid?.() || (metadata.mode & 0o7777) !== 0o600) {
    throw new Error('source SDK requires an inherited private plan descriptor');
  }
  try { return JSON.parse(readFileSync(descriptor, 'utf8')) as ISourcePlan; }
  finally { closeSync(descriptor); }
}

const plan = inheritedPlan();
const pipeline = new OwnedSdkPipeline(plan, { language: plan.language, instanceId: plan.launch_id });
const reader = new SourceHandoffReader();
const metrics = new Map<string, Counter | Histogram | Gauge>();
const report = openSync(path.join(plan.runtime.directory, 'node-status.json'), 'wx', 0o600);
let descriptor: IControlDescriptor | undefined;
let checkpoint: SourceCheckpointType | undefined;
let failed = false;
let stopping = false;
let spansReceived = 0;

function createInstrument(record: ISourceMetric): Counter | Histogram | Gauge {
  const meter = pipeline.meterProvider.getMeter(record.scope, record.version, { schemaUrl: record.schemaUrl });
  switch (record.instrument) {
    case 'counter': return meter.createCounter(record.name, record.options);
    case 'histogram': return meter.createHistogram(record.name, record.options);
    case 'gauge': return meter.createGauge(record.name, record.options);
  }
}

function recordMetric(record: ISourceMetric): void {
  const key = JSON.stringify([record.scope, record.version, record.schemaUrl, record.instrument, record.name, record.options]);
  let instrument = metrics.get(key);
  if (!instrument) {
    instrument = createInstrument(record);
    metrics.set(key, instrument);
  }
  const active = record.context ? trace.setSpanContext(ROOT_CONTEXT, contextFromWire(record.context)) : ROOT_CONTEXT;
  if ('add' in instrument) instrument.add(record.value, record.attributes, active);
  else instrument.record(record.value, record.attributes, active);
}

function status(): object {
  return { ...pipeline.status(), sdk_pid: process.pid, handoff_received: reader.accepted, handoff_incomplete: reader.lost };
}

async function consume(record: SourceSdkRecordType): Promise<void> {
  switch (record.kind) {
    case 'metric': recordMetric(record); break;
    case 'span': spansReceived++; pipeline.acceptSpan(spanFromWire(record.span)); break;
    case 'controls':
      pipeline.controls.setCollection(record.collection); pipeline.controls.setEmission(record.emission); break;
    case 'checkpoint': checkpoint = record.status; break;
    case 'control-descriptor': descriptor = record.descriptor; break;
    case 'request': {
      let ok = true;
      try {
        if (record.command === 'flush') await pipeline.flush();
        if (record.command === 'shutdown') { stopping = true; await pipeline.shutdown(); }
      } catch { ok = false; }
      if (process.connected) process.send?.({ id: record.id, ok, status: status() }, () => {
        // The application can exit while this control operation is draining.
        // A disconnected caller must not interrupt the independent telemetry drain.
      });
      break;
    }
  }
}

function consumeBatch(chunk: Buffer): Promise<boolean> {
  // Commands are barriers in the FIFO, never parallel export operations. A
  // shutdown completes its current decoded batch and refuses subsequent ones.
  if (stopping) return Promise.resolve(true);
  return reader.push(chunk).reduce<Promise<void>>((previous, record) =>
    previous.then(() => consume(record as SourceSdkRecordType)), Promise.resolve()).then(() => stopping);
}

const input = openSync(path.join(plan.runtime.directory, 'node-events.fifo'), constants.O_RDONLY | constants.O_NOFOLLOW);
const metadata = fstatSync(input);
if (!metadata.isFIFO() || metadata.uid !== process.getuid?.() || (metadata.mode & 0o7777) !== 0o600) {
  closeSync(input); throw new Error('source SDK input is not an owned private FIFO');
}
publishDescriptor(path.join(plan.runtime.directory, 'node-sdk-ready.json'),
  JSON.stringify({ schema_version: 1, pid: process.pid, launch_id: plan.launch_id }) + '\n');
// Parent disconnect is not a completion signal: buffered FIFO bytes still belong
// to the application. Only actual EOF establishes the end of the record stream.
process.on('disconnect', () => { /* Drain the FIFO before the SDK. */ });
try {
  const batches = createReadStream('', { fd: input, autoClose: true })
    .map(consumeBatch, { concurrency: 1 });
  for await (const stopped of batches) {
    if (stopped) break;
  }
  reader.finish();
} catch { failed = true; }
try { await pipeline.shutdown(); } catch { failed = true; }
try { if (descriptor) removeControlDescriptor(descriptor); } catch { failed = true; }
try {
  const sdk = pipeline.status();
  const queueKnown = sdk.phase === 'closed' && sdk.traces.failed === 0 && sdk.traces.pending === 0;
  const dropped = queueKnown ? spansReceived - sdk.traces.exported_items : null;
  // Actual EOF means application objects can no longer complete. Keep their last
  // live counts as censored observations, never invent GC/release timestamps.
  if (checkpoint) {
    checkpoint.runtime_losses.incomplete += checkpoint.active_functions;
    if (checkpoint.lifetimes) {
      checkpoint.lifetimes.censored = checkpoint.lifetimes.current_live;
      checkpoint.lifetimes.tracked_instances = 0;
      checkpoint.lifetimes.tracked_constructors = 0;
    }
    failed ||= checkpoint.handoff.lost > 0 || checkpoint.native_callback_failures > 0
      || checkpoint.lifetime_lost > 0 || Object.values(checkpoint.runtime_losses).some(value => value > 0)
      || checkpoint.span_observation.handed_off !== spansReceived || checkpoint.span_observation.sampled_active > 0;
  }
  failed ||= dropped !== null && dropped !== 0;
  const final = { ...status(), ...checkpoint,
    sdk_phase: pipeline.status().phase,
    span_accounting: { received: spansReceived, sdk_exported: sdk.traces.exported_items,
      queue_evidence: queueKnown ? 'derived_after_shutdown' : 'unavailable',
      queue_pending: queueKnown ? 0 : null, queue_dropped: dropped,
      downstream_receipt: 'unavailable', otlp_partial_rejection: 'unavailable' },
    phase: failed || reader.lost || !checkpoint ? 'failed' : pipeline.status().phase,
    producer_checkpoint_available: checkpoint !== undefined };
  const data = Buffer.from(JSON.stringify(final) + '\n');
  ftruncateSync(report, 0); writeSync(report, data, 0, data.length, 0);
} finally { closeSync(report); }
if (process.connected) process.disconnect?.();
