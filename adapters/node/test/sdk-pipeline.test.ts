import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { chmodSync, mkdtempSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { connect } from 'node:net';
import { once } from 'node:events';
import type { Worker } from 'node:worker_threads';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { describe, it } from 'node:test';
import type { TestContext } from 'node:test';
import { ROOT_CONTEXT, trace, TraceFlags } from '@opentelemetry/api';
import { LocalMetricControl } from '../src/local-metric-control.ts';
import { OwnedSdkPipeline } from '../src/sdk-pipeline.ts';
import type { ISdkPolicy } from '../src/sdk-pipeline.ts';

async function collector(t: TestContext) {
  const requests: Array<{ path: string; body: Buffer }> = [];
  let failMetrics = false;
  let retryMetricAttempts = 0;
  let holdTraces = false;
  const traceSeen = Promise.withResolvers<void>();
  const heldResponses: Array<import('node:http').ServerResponse> = [];
  const cleanups: Array<() => void | Promise<void>> = [];
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on('data', chunk => chunks.push(chunk));
    request.once('end', () => {
      requests.push({ path: request.url!, body: Buffer.concat(chunks) });
      assert.equal(request.headers['content-type'], 'application/x-protobuf');
      if (holdTraces && request.url === '/v1/traces') {
        heldResponses.push(response);
        traceSeen.resolve();
        return;
      }
      let status = failMetrics && request.url === '/v1/metrics' ? 400 : 200;
      if (request.url === '/v1/metrics' && retryMetricAttempts > 0) { retryMetricAttempts--; status = 503; }
      response.writeHead(status);
      response.end();
    });
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    try { for (const cleanup of cleanups.reverse()) await cleanup(); }
    finally { await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections(); }); }
  });
  const address = server.address();
  assert(address && typeof address !== 'string');
  const policy: ISdkPolicy = { resource: { service_name: 'owned-sdk-test' },
    metrics: { collection: false, emission: false, histogram_boundaries_seconds: [.001, .01, .1] },
    traces: { enabled: true, sample_ratio: 1 },
    export: { endpoint: `http://127.0.0.1:${address.port}`, interval_ms: 60_000,
      timeout_ms: 1000, shutdown_timeout_ms: 2000 } };
  return { policy, requests, failMetrics: (value: boolean) => { failMetrics = value; },
    retryMetricsOnce: () => { retryMetricAttempts = 1; },
    cleanup: (operation: () => void | Promise<void>) => { cleanups.push(operation); },
    traceSeen: traceSeen.promise,
    holdTraces: () => { holdTraces = true; },
    releaseTraces: () => { holdTraces = false; for (const response of heldResponses) response.end(); } };
}

async function rpc(filename: string, command: string, alteredToken = false): Promise<Record<string, unknown>> {
  // Test-only reader; do not include private descriptor contents in assertion output.
  const descriptor = JSON.parse(readFileSync(filename, 'utf8')) as { host: string; port: number; token: string };
  const socket = connect(descriptor.port, descriptor.host);
  return new Promise((resolve, reject) => {
    let reply = '';
    socket.once('error', reject);
    socket.once('connect', () => socket.write(JSON.stringify({ token: alteredToken ? '0'.repeat(64) : descriptor.token, command }) + '\n'));
    socket.on('data', chunk => { reply += chunk.toString(); });
    socket.once('end', () => resolve(JSON.parse(reply) as Record<string, unknown>));
  });
}

describe('official owned SDK pipeline', () => {
  it('fails an interrupted export honestly and drains every pending worker acknowledgement', async t => {
    const c = await collector(t);
    c.holdTraces();
    const created = once(process, 'worker');
    const pipeline = new OwnedSdkPipeline(c.policy);
    const [worker] = await created as [Worker];
    pipeline.tracerProvider.getTracer('fixture').startSpan('interrupted').end();
    const failed = assert.rejects(pipeline.flush(), /owned OpenTelemetry SDK/);
    await c.traceSeen;
    assert.equal(pipeline.status().traces.pending, 1);
    await worker.terminate();
    await failed;
    assert.equal(pipeline.status().phase, 'failed');
    assert.equal(pipeline.status().traces.pending, 0);
    assert.equal(pipeline.status().traces.failed, 1);
    await assert.rejects(pipeline.shutdown(), /owned OpenTelemetry SDK/);
  });

  it('refuses excess concurrent flush work without leaving the SDK unusable', async t => {
    const c = await collector(t);
    const pipeline = new OwnedSdkPipeline(c.policy);
    c.cleanup(() => pipeline.shutdown());
    const outcomes = await Promise.allSettled(Array.from({ length: 256 }, () => pipeline.flush()));
    assert(outcomes.some(result => result.status === 'fulfilled'));
    assert(outcomes.some(result => result.status === 'rejected'));
    await pipeline.flush();
    assert.equal(pipeline.status().phase, 'running');
  });

  it('does not advance an application random generator when creating trace identities', async t => {
    const c = await collector(t);
    const pipeline = new OwnedSdkPipeline(c.policy);
    c.cleanup(() => pipeline.shutdown());
    const original = Math.random;
    let calls = 0;
    try {
      Math.random = () => { calls++; return .25; };
      const first = pipeline.tracerProvider.getTracer('fixture').startSpan('first');
      const second = pipeline.tracerProvider.getTracer('fixture').startSpan('second');
      first.end(); second.end();
      await pipeline.flush();
      assert.equal(calls, 0);
      assert.notEqual(first.spanContext().traceId, second.spanContext().traceId);
    } finally { Math.random = original; }
  });

  it('does not advance an application random generator during official exporter retries', async t => {
    const c = await collector(t);
    c.policy.metrics.collection = true; c.policy.metrics.emission = true;
    c.policy.export.timeout_ms = 3000;
    c.retryMetricsOnce();
    const pipeline = new OwnedSdkPipeline(c.policy);
    c.cleanup(() => pipeline.shutdown());
    const original = Math.random;
    let calls = 0;
    try {
      Math.random = () => { calls++; return .25; };
      pipeline.meterProvider.getMeter('fixture').createCounter('calls').add(1);
      await pipeline.flush();
      assert.equal(c.requests.filter(request => request.path === '/v1/metrics').length, 2);
      assert.equal(pipeline.status().export_succeeded, 1);
      assert.equal(calls, 0);
    } finally { Math.random = original; }
  });

  it('keeps traces independent across all four metric states and reports real exporter responses', async t => {
    const c = await collector(t);
    const pipeline = new OwnedSdkPipeline(c.policy);
    c.cleanup(() => pipeline.shutdown());
    const counter = pipeline.meterProvider.getMeter('fixture').createCounter('cached.counter');
    const tracer = pipeline.tracerProvider.getTracer('fixture');
    const modes = [[false, false], [true, true], [true, false], [false, true], [true, true]];
    const metricRequests = [0, 1, 1, 1, 2];
    for (const [index, [collection, emission]] of modes.entries()) {
      pipeline.controls.setCollection(collection);
      pipeline.controls.setEmission(emission);
      counter.add(1);
      tracer.startSpan(`mode-${index}`).end();
      await pipeline.flush();
      assert.equal(c.requests.filter(request => request.path === '/v1/metrics').length, metricRequests[index]);
      assert.equal(c.requests.filter(request => request.path === '/v1/traces').length, index + 1);
      assert.equal(pipeline.status().pid, process.pid);
    }
    assert.equal(pipeline.status().export_succeeded, 2);
    assert.equal(pipeline.status().export_failed, 0);
    assert.equal(pipeline.status().phase, 'running');
    await pipeline.shutdown();
    assert.equal(pipeline.status().phase, 'closed');
  });

  it('recovers a failed metric export without stopping successful trace delivery', async t => {
    const c = await collector(t);
    c.policy.metrics = { ...c.policy.metrics, collection: true, emission: true };
    const pipeline = new OwnedSdkPipeline(c.policy);
    c.cleanup(() => pipeline.shutdown());
    pipeline.meterProvider.getMeter('fixture').createCounter('calls').add(2);
    c.failMetrics(true);
    pipeline.tracerProvider.getTracer('fixture').startSpan('during-failure').end();
    await assert.rejects(pipeline.flush(), /owned OpenTelemetry SDK collection\/export failed or remains pending/);
    assert.equal(pipeline.status().export_failed, 1);
    assert.equal(c.requests.filter(request => request.path === '/v1/traces').length, 1);
    c.failMetrics(false);
    await pipeline.flush();
    assert.equal(pipeline.status().export_succeeded, 1);
    assert.equal(pipeline.status().in_flight_exports, 0);
  });

  it('does not report successful flush when an observable collection fails before export', async t => {
    const c = await collector(t);
    c.policy.metrics.collection = true;
    c.policy.metrics.emission = true;
    const pipeline = new OwnedSdkPipeline(c.policy);
    c.cleanup(() => pipeline.shutdown());
    const observable = pipeline.meterProvider.getMeter('fixture').createObservableGauge('broken.callback');
    const callback = () => { throw new Error('controlled collection failure'); };
    observable.addCallback(callback);
    try { await assert.rejects(pipeline.flush(), /owned OpenTelemetry SDK/); }
    finally { observable.removeCallback(callback); }
    assert.equal(pipeline.status().metric_collection.failed, 1);
    await pipeline.flush();
  });

  it('supports tracing disabled and reports the pinned reader collection budget', async t => {
    const c = await collector(t);
    c.policy.traces.enabled = false;
    c.policy.export.interval_ms = 1000;
    c.policy.export.timeout_ms = 2000;
    const pipeline = new OwnedSdkPipeline(c.policy);
    pipeline.tracerProvider.getTracer('fixture').startSpan('not-recorded').end();
    await pipeline.flush();
    assert.equal(c.requests.length, 0);
    assert.equal(pipeline.status().reader_timeout_ms, 1000);
    await Promise.all([pipeline.shutdown(), pipeline.shutdown()]);
    await assert.rejects(pipeline.flush(), /owned SDK pipeline is not running/);
  });

  it('honours parent sampling while enabled and refuses sampled parents when tracing is disabled', async t => {
    const c = await collector(t);
    const parent = trace.setSpanContext(ROOT_CONTEXT, { traceId: '1'.repeat(32), spanId: '2'.repeat(16),
      isRemote: true, traceFlags: TraceFlags.SAMPLED });
    c.policy.traces.sample_ratio = 0;
    for (const enabled of [true, false]) {
      c.policy.traces.enabled = enabled;
      const pipeline = new OwnedSdkPipeline(c.policy);
      const span = pipeline.tracerProvider.getTracer('fixture').startSpan('parented', {}, parent);
      assert.equal(span.isRecording(), enabled);
      assert.equal(span.spanContext().traceId, '1'.repeat(32));
      span.end();
      await pipeline.flush();
      assert.equal(pipeline.status().traces.exported_items, enabled ? 1 : 0);
      await pipeline.shutdown();
    }
  });

  it('retains a failed phase when the caller deadline expires before SDK export cleanup', async t => {
    const c = await collector(t);
    c.policy.export.shutdown_timeout_ms = 100;
    c.holdTraces();
    const pipeline = new OwnedSdkPipeline(c.policy);
    pipeline.tracerProvider.getTracer('fixture').startSpan('pending-on-shutdown').end();
    const timedOut = assert.rejects(pipeline.shutdown(), /owned SDK shutdown deadline exceeded/);
    await c.traceSeen;
    await timedOut;
    assert.equal(pipeline.status().phase, 'failed');
    assert.equal(pipeline.status().traces.pending, 1);
    c.releaseTraces();
    await pipeline.tracerProvider.forceFlush();
    assert.equal(pipeline.status().traces.pending, 0);
    assert.equal(pipeline.status().phase, 'failed', 'late completion must not upgrade a failed shutdown');
  });
});

describe('private loopback metric controls', () => {
  it('controls each flag, authenticates, and closes idle peers without hanging shutdown', async t => {
    const c = await collector(t);
    const pipeline = new OwnedSdkPipeline(c.policy);
    c.cleanup(() => pipeline.shutdown());
    const directory = mkdtempSync(path.join(tmpdir(), 'otelc-control-'));
    t.after(() => rmSync(directory, { recursive: true, force: true }));
    const control = await LocalMetricControl.listen(directory, pipeline);
    c.cleanup(() => control.close());
    assert.equal(statSync(control.filename).mode & 0o077, 0);
    assert.equal(control.transportFailed, false);
    assert.equal((await rpc(control.filename, 'status')).pid, process.pid);
    assert.equal((await rpc(control.filename, 'emission on')).collection, false);
    assert.equal((await rpc(control.filename, 'collection on')).emission, true);
    assert.equal((await rpc(control.filename, 'off')).emission, false);
    assert.equal((await rpc(control.filename, 'on')).collection, true);
    assert.equal((await rpc(control.filename, 'collection off')).collection, false);
    assert.equal((await rpc(control.filename, 'emission off')).emission, false);
    assert.equal((await rpc(control.filename, 'flush')).in_flight_exports, 0);
    assert.equal((await rpc(control.filename, 'status', true)).error, 'invalid control request');
    assert.equal((await rpc(control.filename, 'restart')).error, 'invalid control request');
    const descriptor = JSON.parse(readFileSync(control.filename, 'utf8')) as { host: string; port: number };
    const idle = connect(descriptor.port, descriptor.host);
    idle.on('error', (error: NodeJS.ErrnoException) => { assert.equal(error.code, 'ECONNRESET'); });
    await new Promise<void>(resolve => idle.once('connect', resolve));
    const closed = new Promise<void>(resolve => idle.once('close', () => resolve()));
    await Promise.all([control.close(), control.close(), closed]);
    assert.equal(pipeline.status().phase, 'running', 'closing controls must not close either SDK signal');
  });

  it('can close before pending control publication without leaving an endpoint', async t => {
    const c = await collector(t);
    const pipeline = new OwnedSdkPipeline(c.policy);
    c.cleanup(() => pipeline.shutdown());
    const directory = mkdtempSync(path.join(tmpdir(), 'otelc-pending-control-'));
    t.after(() => rmSync(directory, { recursive: true, force: true }));
    const control = LocalMetricControl.start(directory, pipeline);
    const closed = control.close();
    await assert.rejects(control.ready, /closed before publication/);
    await closed;
    assert.throws(() => statSync(control.filename), { code: 'ENOENT' });
    assert.equal(control.transportFailed, false);
  });

  it('preserves a replaced or edited descriptor and refuses an occupied path', async t => {
    const c = await collector(t);
    const pipeline = new OwnedSdkPipeline(c.policy);
    c.cleanup(() => pipeline.shutdown());
    for (const replace of [false, true]) {
      const directory = mkdtempSync(path.join(tmpdir(), 'otelc-control-'));
      t.after(() => rmSync(directory, { recursive: true, force: true }));
      const control = await LocalMetricControl.listen(directory, pipeline);
      if (replace) renameSync(control.filename, control.filename + '.previous');
      writeFileSync(control.filename, 'application-owned replacement', { mode: 0o600 });
      await control.close();
      assert.equal(readFileSync(control.filename, 'utf8'), 'application-owned replacement');
      await assert.rejects(LocalMetricControl.listen(directory, pipeline));
      assert.equal(readFileSync(control.filename, 'utf8'), 'application-owned replacement');
    }
    const publicDirectory = mkdtempSync(path.join(tmpdir(), 'otelc-public-'));
    t.after(() => rmSync(publicDirectory, { recursive: true, force: true }));
    chmodSync(publicDirectory, 0o755);
    await assert.rejects(LocalMetricControl.listen(publicDirectory, pipeline), /owned and mode 0700/);
  });

  it('rejects malformed frames and bounds incomplete or oversized control peers', async t => {
    const c = await collector(t);
    const pipeline = new OwnedSdkPipeline(c.policy);
    c.cleanup(() => pipeline.shutdown());
    const directory = mkdtempSync(path.join(tmpdir(), 'otelc-frames-'));
    t.after(() => rmSync(directory, { recursive: true, force: true }));
    const control = await LocalMetricControl.listen(directory, pipeline);
    c.cleanup(() => control.close());
    const descriptor = JSON.parse(readFileSync(control.filename, 'utf8')) as { host: string; port: number };
    for (const payload of ['not-json\n', '[]\n', '{}\n', '{"token":1,"command":"status"}\n',
                           '{"token":"short","command":"status"}\n', 'null\n', 'status\nextra\n',
                           'x'.repeat(1025), 'incomplete']) {
      const socket = connect(descriptor.port, descriptor.host);
      let reply = '';
      socket.on('error', (error: NodeJS.ErrnoException) => assert.equal(error.code, 'ECONNRESET'));
      socket.on('data', chunk => { reply += chunk.toString(); });
      const closed = new Promise<void>(resolve => socket.once('close', () => resolve()));
      await new Promise<void>(resolve => socket.once('connect', resolve));
      socket.write(payload);
      await closed;
      if (reply) assert.equal((JSON.parse(reply) as { error: string }).error, 'invalid control request');
    }
    assert.equal(pipeline.status().collection, false);
    assert.equal(pipeline.status().emission, false);
  });
});
