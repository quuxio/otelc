import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import type { TestContext } from 'node:test';
import { ExportResultCode } from '@opentelemetry/core';
import type { ExportResult } from '@opentelemetry/core';
import { resourceFromAttributes } from '@opentelemetry/resources';
import {
  AggregationTemporality, AggregationType, InMemoryMetricExporter, InstrumentType,
  MeterProvider, PeriodicExportingMetricReader,
} from '@opentelemetry/sdk-metrics';
import type { PushMetricExporter, ResourceMetrics } from '@opentelemetry/sdk-metrics';
import { MetricControls } from '../src/metric-controls.ts';
import { GatedMeterProvider } from '../src/gated-meter-provider.ts';
import { GatedMetricExporter } from '../src/gated-metric-exporter.ts';

function fixture(t: TestContext, enabled = false) {
  const controls = new MetricControls(enabled, enabled);
  const memory = new InMemoryMetricExporter(AggregationTemporality.CUMULATIVE);
  const exporter = new GatedMetricExporter(memory, controls);
  const reader = new PeriodicExportingMetricReader({ exporter, exportIntervalMillis: 60_000, exportTimeoutMillis: 1000 });
  const sdk = new MeterProvider({ readers: [reader] });
  const provider = new GatedMeterProvider(sdk, controls);
  t.after(() => sdk.shutdown());
  return { controls, memory, exporter, reader, sdk, provider, meter: provider.getMeter('owned', '1', { schemaUrl: 'schema:test' }) };
}

function point(data: ResourceMetrics, name: string) {
  const metric = data.scopeMetrics.flatMap(scope => scope.metrics).find(metric => metric.descriptor.name === name);
  assert(metric, `missing metric ${name}`);
  assert.equal(metric.dataPoints.length, 1);
  return metric.dataPoints[0].value;
}

describe('public SDK collection gates', () => {
  it('gates cached and new counters, histograms, up-down counters and gauges', async t => {
    const f = fixture(t);
    assert.equal(f.provider.getMeter('owned', '1', { schemaUrl: 'schema:test' }), f.meter);
    const counter = f.meter.createCounter('calls');
    const upDown = f.meter.createUpDownCounter('live');
    const histogram = f.meter.createHistogram('latency');
    const gauge = f.meter.createGauge('gauge');
    let attributesRead = 0;
    const attributes = new Proxy({}, { ownKeys() { attributesRead++; return []; } });
    counter.add(100, attributes); upDown.add(100); histogram.record(100); gauge.record(100);
    assert.equal(attributesRead, 0, 'disabled recording must not inspect application attributes');
    f.controls.setCollection(true);
    counter.add(2); upDown.add(3); histogram.record(.25); gauge.record(7);
    const first = (await f.reader.collect()).resourceMetrics;
    assert.equal(point(first, 'calls'), 2);
    assert.equal(point(first, 'live'), 3);
    assert.equal(point(first, 'gauge'), 7);
    const firstHistogram = point(first, 'latency');
    assert(typeof firstHistogram === 'object');
    assert.equal(firstHistogram.count, 1);
    assert.equal(firstHistogram.sum, .25);
    assert.equal(first.scopeMetrics[0].scope.name, 'owned');
    assert.equal(first.scopeMetrics[0].scope.version, '1');
    assert.equal(first.scopeMetrics[0].scope.schemaUrl, 'schema:test');
    f.controls.setCollection(false);
    counter.add(100); upDown.add(100); histogram.record(100); gauge.record(100);
    const newer = f.meter.createCounter('created.off');
    newer.add(100);
    f.controls.setCollection(true);
    newer.add(1); counter.add(3); upDown.add(-1); histogram.record(.5); gauge.record(9);
    const final = (await f.reader.collect()).resourceMetrics;
    assert.equal(point(final, 'calls'), 5);
    assert.equal(point(final, 'live'), 2);
    assert.equal(point(final, 'gauge'), 9);
    assert.equal(point(final, 'created.off'), 1);
    const value = point(final, 'latency');
    assert(typeof value === 'object');
    assert(value && 'count' in value);
    assert.equal(value.count, 2);
  });

  it('skips observable callbacks while off and retains SDK duplicate/removal behaviour', async t => {
    const f = fixture(t);
    const counter = f.meter.createObservableCounter('observable.counter');
    const gauge = f.meter.createObservableGauge('observable.gauge');
    const upDown = f.meter.createObservableUpDownCounter('observable.updown');
    let calls = 0;
    const callback = (result: Parameters<Parameters<typeof counter.addCallback>[0]>[0]) => { calls++; result.observe(7); };
    counter.addCallback(callback); counter.addCallback(callback);
    gauge.addCallback(result => result.observe(8));
    upDown.addCallback(result => result.observe(-2));
    await f.reader.collect();
    assert.equal(calls, 0);
    f.controls.setCollection(true);
    const first = (await f.reader.collect()).resourceMetrics;
    assert.equal(calls, 1);
    assert.equal(point(first, 'observable.counter'), 7);
    assert.equal(point(first, 'observable.gauge'), 8);
    assert.equal(point(first, 'observable.updown'), -2);
    counter.removeCallback(() => {});
    counter.removeCallback(callback);
    await f.reader.collect();
    assert.equal(calls, 1);
    counter.addCallback(callback);
    await f.reader.collect();
    assert.equal(calls, 2);
    f.controls.setCollection(false);
    await f.reader.collect();
    assert.equal(calls, 2);
  });

  it('unwraps batch instruments across scopes and delegates set-based registration to the SDK', async t => {
    const f = fixture(t);
    const first = f.meter.createObservableGauge('batch.first');
    const otherMeter = f.provider.getMeter('other');
    const second = otherMeter.createObservableGauge('batch.second');
    let calls = 0;
    const callback: Parameters<typeof f.meter.addBatchObservableCallback>[0] = result => {
      calls++; result.observe(first, 2); result.observe(second, 3);
    };
    f.meter.removeBatchObservableCallback(callback, [first]);
    f.meter.addBatchObservableCallback(callback, [first, second, first]);
    f.meter.addBatchObservableCallback(callback, [second, first]);
    await f.reader.collect();
    assert.equal(calls, 0);
    f.controls.setCollection(true);
    await f.reader.collect();
    assert.equal(calls, 1);
    // A second collection includes the other scope regardless of SDK scope traversal order.
    const final = (await f.reader.collect()).resourceMetrics;
    assert.equal(calls, 2);
    assert.equal(point(final, 'batch.first'), 2);
    assert.equal(point(final, 'batch.second'), 3);
    f.meter.removeBatchObservableCallback(callback, [first]);
    await f.reader.collect(); assert.equal(calls, 3);
    f.meter.removeBatchObservableCallback(callback, [second, first]);
    await f.reader.collect(); assert.equal(calls, 3);
  });

  it('drops late async observations across off/on without invoking new callbacks while off', async t => {
    const f = fixture(t, true);
    const observable = f.meter.createObservableGauge('late');
    const entered = Promise.withResolvers<void>();
    const release = Promise.withResolvers<void>();
    let calls = 0;
    const callback = async (result: Parameters<Parameters<typeof observable.addCallback>[0]>[0]) => {
      calls++; entered.resolve(); await release.promise; result.observe(99);
    };
    observable.addCallback(callback);
    const collecting = f.reader.collect();
    await entered.promise;
    f.controls.setCollection(false);
    f.controls.setCollection(true);
    release.resolve();
    const final = (await collecting).resourceMetrics;
    assert.equal(final.scopeMetrics.flatMap(scope => scope.metrics).filter(metric => metric.dataPoints.length).length, 0);
    assert.equal(calls, 1);
    observable.removeCallback(callback);
    f.controls.setCollection(false);
    await f.reader.collect();
    assert.equal(calls, 1);
  });

  it('keeps cumulative measurements through emission-off and exports no collection-off snapshot', async t => {
    const f = fixture(t);
    const counter = f.meter.createCounter('calls');
    counter.add(100);
    await f.sdk.forceFlush();
    assert.equal(f.memory.getMetrics().length, 0);
    f.controls.setCollection(true);
    counter.add(2);
    await f.sdk.forceFlush();
    assert.equal(f.memory.getMetrics().length, 0);
    f.controls.setEmission(true);
    await f.sdk.forceFlush();
    assert.equal(point(f.memory.getMetrics().at(-1)!, 'calls'), 2);
    f.controls.setCollection(false);
    counter.add(100);
    const received = f.memory.getMetrics().length;
    await f.sdk.forceFlush();
    assert.equal(f.memory.getMetrics().length, received);
    f.controls.setCollection(true);
    counter.add(3);
    await f.sdk.forceFlush();
    assert.equal(point(f.memory.getMetrics().at(-1)!, 'calls'), 5);
  });

  it('invalidates late batch observations without losing original callback removal', async t => {
    const f = fixture(t, true);
    const observable = f.meter.createObservableGauge('late.batch');
    const entered = Promise.withResolvers<void>();
    const release = Promise.withResolvers<void>();
    const callback: Parameters<typeof f.meter.addBatchObservableCallback>[0] = async result => {
      entered.resolve(); await release.promise; result.observe(observable, 99);
    };
    f.meter.addBatchObservableCallback(callback, [observable]);
    const pending = f.reader.collect();
    await entered.promise;
    f.controls.setCollection(false);
    f.controls.setCollection(true);
    release.resolve();
    const final = (await pending).resourceMetrics;
    assert.equal(final.scopeMetrics.flatMap(scope => scope.metrics).filter(metric => metric.dataPoints.length).length, 0);
    f.meter.removeBatchObservableCallback(callback, [observable]);
  });
});

class DeferredExporter implements PushMetricExporter {
  callbacks: Array<(result: ExportResult) => void> = [];
  flushed = 0;
  stopped = 0;
  export(_metrics: ResourceMetrics, callback: (result: ExportResult) => void): void { this.callbacks.push(callback); }
  async forceFlush(): Promise<void> { this.flushed++; }
  async shutdown(): Promise<void> { this.stopped++; }
}

const EMPTY: ResourceMetrics = { resource: resourceFromAttributes({}), scopeMetrics: [] };

describe('independent SDK emission gate', () => {
  it('retains an honest off barrier until admitted requests settle and ignores duplicate acknowledgements', async () => {
    const controls = new MetricControls(true, true);
    const transport = new DeferredExporter();
    const exporter = new GatedMetricExporter(transport, controls);
    let replies = 0;
    exporter.export(EMPTY, result => { assert.equal(result.code, ExportResultCode.SUCCESS); replies++; });
    controls.setEmission(false);
    assert.equal(exporter.status().in_flight_exports, 1);
    assert.equal(exporter.status().effective_emission_off, false);
    exporter.export(EMPTY, () => { replies++; });
    assert.equal(transport.callbacks.length, 1);
    transport.callbacks[0]({ code: ExportResultCode.SUCCESS });
    transport.callbacks[0]({ code: ExportResultCode.FAILED });
    assert.equal(replies, 2);
    assert.deepEqual(exporter.status(), { schema_version: 1, pid: process.pid, collection: true, emission: false,
      in_flight_exports: 0, effective_emission_off: true, export_started: 1, export_succeeded: 1, export_failed: 0 });
    assert.equal(exporter.selectAggregationTemporality(), AggregationTemporality.CUMULATIVE);
    assert.deepEqual(exporter.selectAggregation(InstrumentType.COUNTER), { type: AggregationType.DEFAULT });
    await exporter.forceFlush(); assert.equal(transport.flushed, 1);
    await Promise.all([exporter.shutdown(), exporter.shutdown()]);
    assert.equal(transport.stopped, 1);
    exporter.export(EMPTY, result => assert.equal(result.code, ExportResultCode.FAILED));
  });

  it('records synchronous transport failure without leaving a stuck barrier', () => {
    const controls = new MetricControls(true, true);
    const transport = new DeferredExporter();
    transport.export = () => { throw new Error('controlled transport failure'); };
    const exporter = new GatedMetricExporter(transport, controls);
    exporter.export(EMPTY, result => assert.equal(result.code, ExportResultCode.FAILED));
    controls.setEmission(false);
    assert.equal(exporter.status().effective_emission_off, true);
    assert.equal(exporter.status().export_failed, 1);
  });
});
