import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import type { ServerResponse } from 'node:http';
import { setTimeout as delay } from 'node:timers/promises';
import { it } from 'node:test';
import { OwnedSdkPipeline } from '../src/sdk-pipeline.ts';

it('allows a fresh periodic export after the metric flush barrier while traces finish', { timeout: 10_000 }, async t => {
  const firstMetric = Promise.withResolvers<void>();
  const periodicMetric = Promise.withResolvers<void>();
  const traceSeen = Promise.withResolvers<void>();
  let metricRequests = 0;
  let holding = true;
  let traceResponse: ServerResponse | undefined;
  const metricResponses: ServerResponse[] = [];
  const server = createServer((request, response) => {
    request.resume();
    request.once('end', () => {
      if (request.url === '/v1/metrics') {
        metricRequests++;
        if (holding && metricRequests === 3) {
          metricResponses.push(response); periodicMetric.resolve(); return;
        }
        response.end();
        if (metricRequests === 1) firstMetric.resolve();
        return;
      }
      if (holding && request.url === '/v1/traces') {
        traceResponse = response; traceSeen.resolve(); return;
      }
      response.end();
    });
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert(address && typeof address !== 'string');
  const pipeline = new OwnedSdkPipeline({ resource: { service_name: 'periodic-flush-fixture' },
    metrics: { collection: true, emission: true, histogram_boundaries_seconds: [.001, .01] },
    traces: { enabled: true, sample_ratio: 1 },
    export: { endpoint: `http://127.0.0.1:${address.port}`, interval_ms: 1000,
      timeout_ms: 2000, shutdown_timeout_ms: 3000 } });
  t.after(async () => {
    holding = false;
    traceResponse?.end();
    for (const response of metricResponses) response.end();
    try { await pipeline.shutdown(); }
    finally { await new Promise<void>(resolve => { server.close(() => resolve()); server.closeAllConnections(); }); }
  });
  pipeline.meterProvider.getMeter('fixture').createCounter('work').add(1);
  await firstMetric.promise;
  // Start between periodic ticks. Hold traces until a subsequent periodic metric
  // request proves that the earlier explicit metric flush has already completed.
  await delay(400);
  pipeline.tracerProvider.getTracer('fixture').startSpan('work').end();
  const flushed = pipeline.flush();
  const accepted = assert.doesNotReject(flushed);
  await traceSeen.promise;
  await periodicMetric.promise;
  traceResponse!.end();
  await accepted;
  const active = pipeline.status();
  assert.equal(active.in_flight_exports, 1);
  assert.equal(active.export_failed, 0);
  assert.equal(active.traces.failed, 0);
  assert.equal(active.metric_collection.failed, 0);
  assert.equal(active.phase, 'running');
  let closed = false;
  const stopping = pipeline.shutdown().then(() => { closed = true; });
  await delay(20);
  assert.equal(closed, false, 'shutdown must still wait for the held periodic request');
  holding = false;
  for (const response of metricResponses) response.end();
  await stopping;
  const final = pipeline.status();
  assert.equal(final.phase, 'closed');
  assert.equal(final.in_flight_exports, 0);
  assert.equal(final.export_failed, 0);
});
