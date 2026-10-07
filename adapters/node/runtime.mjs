import fs from 'node:fs';
import path from 'node:path';
import net from 'node:net';
import { MeterProvider, PeriodicExportingMetricReader, AggregationType } from '@opentelemetry/sdk-metrics';
import { resourceFromAttributes } from '@opentelemetry/resources';
import { ValueType } from '@opentelemetry/api';
import { Exporter } from './exporter.mjs';
import { PromiseObserver } from './promise-observer.mjs';
import { TraceStore, SUPPRESSED } from './traces.mjs';

export const RUNTIME = Symbol.for('quux.otelc.runtime');
export class Runtime {
  constructor(plan) {
    this.plan = plan;
    this.enabled = plan.metrics.enabled;
    this.functions = new Map();
    this.pending = new Map();
    this.next = 1;
    this.losses = { function_capacity: 0, active_call_capacity: 0, incomplete: 0, invalid: 0, async_origin: 0 };
    this.promiseObserver = null;
    this.exportLoss = 0;
    this.timeoutMs = Math.min(plan.export.timeout_ms, plan.runtime.shutdown_timeout_ms, plan.export.interval_ms);
    this.closed = false;
    this.current = null;
    this.shutdownDeadline = 1n << 127n;
    this.exportFinished = false;
    this.server = null;
    this.connections = new Set();
    this.socketInode = null;
    const resource = resourceFromAttributes({ 'service.name': plan.resource.service_name, 'service.version': plan.resource.service_version, 'service.instance.id': String(process.pid), ...plan.resource.attributes });
    this.traces = plan.traces?.enabled ? new TraceStore(this, resource) : null;
    this.exporter = new Exporter(this);
    this.reader = new PeriodicExportingMetricReader({ exporter: this.exporter, exportIntervalMillis: plan.export.interval_ms, exportTimeoutMillis: this.timeoutMs });
    const collect = this.reader.collect.bind(this.reader);
    this.reader.collect = options => { this.promiseObserver?.flush(); return collect(options); };
    const capacity = plan.runtime.max_functions + 1;
    this.provider = new MeterProvider({
      resource,
      readers: [this.reader], views: [
        ...['otelc.function.calls', 'otelc.function.unwinds'].map(instrumentName => ({ instrumentName, aggregationCardinalityLimit: capacity })),
        { instrumentName: 'otelc.function.duration', aggregationCardinalityLimit: capacity, aggregation: { type: AggregationType.EXPLICIT_BUCKET_HISTOGRAM, options: { boundaries: plan.metrics.histogram_boundaries_seconds } } }
      ]
    });
    const meter = this.provider.getMeter('quux.otelc', '0.1.0');
    this.calls = meter.createCounter('otelc.function.calls', { unit: '{call}', valueType: ValueType.INT });
    this.unwinds = meter.createCounter('otelc.function.unwinds', { unit: '{observation}', valueType: ValueType.INT });
    this.duration = meter.createHistogram('otelc.function.duration', { unit: 's' });
    meter.createObservableCounter('otelc.runtime.dropped_observations', { unit: '{observation}', valueType: ValueType.INT }).addCallback(observer => {
      for (const [reason, count] of Object.entries(this.losses)) observer.observe(count, { reason });
    });
    meter.createObservableCounter('otelc.export.dropped_batches', { unit: '{batch}', valueType: ValueType.INT }).addCallback(observer => observer.observe(this.exportLoss));
    if (this.traces) meter.createObservableCounter('otelc.trace.dropped_trees', { unit: '{tree}', valueType: ValueType.INT }).addCallback(observer => {
      for (const [reason, count] of Object.entries(this.traces.losses)) observer.observe(count, { reason });
    });
  }
  get observing() { return this.enabled || Boolean(this.traces); }
  parent() { return this.current === null ? null : this.pending.get(this.current)?.trace ?? SUPPRESSED; }
  rejectTrace(reason) { this.traces?.reject(this.parent(), reason); return 0; }
  attach(token, previous = undefined) {
    if (!this.traces) return null;
    if (this.current === token && previous !== undefined) return previous;
    const caller = this.current; this.current = token; return caller;
  }
  detach(previous) { if (this.traces) this.current = previous; }
  suspend(previous, value) { this.detach(previous); return value; }
  register(name) {
    if (this.functions.has(name)) return true;
    if (this.functions.size >= this.plan.runtime.max_functions || Buffer.byteLength(name) > 1024) { this.losses.function_capacity++; return false; }
    this.functions.set(name, { count: 0, unwinds: 0, attributes: { 'code.function.name': name } });
    return true;
  }
  enter(name) {
    if (!this.observing || this.closed) return 0;
    this.promiseObserver?.flush();
    if (!this.functions.has(name) && !this.register(name)) return this.rejectTrace('function_capacity');
    if (this.pending.size >= this.plan.runtime.max_active_calls) { this.losses.active_call_capacity++; return this.rejectTrace('active_call_capacity'); }
    if (this.next >= Number.MAX_SAFE_INTEGER) { this.losses.invalid++; return this.rejectTrace('invalid'); }
    const token = this.next++;
    const start = process.hrtime.bigint();
    this.pending.set(token, { name, start, metrics: this.enabled, trace: this.traces?.begin(this.parent(), name, start) });
    return token;
  }
  registerAsyncSites(sites) {
    if (!sites.length) return;
    this.promiseObserver ??= new PromiseObserver(this);
    this.promiseObserver.register(sites);
  }
  enterAsync(name, id) {
    try { return this.promiseObserver?.enter(name, id) ?? 0; } catch { this.losses.invalid++; return this.rejectTrace('invalid'); }
  }
  exit(token, unwound, ended = null) {
    if (!token) return;
    const end = ended ?? process.hrtime.bigint();
    const frame = this.pending.get(token);
    if (!frame) return;
    this.pending.delete(token);
    this.traces?.finish(frame.trace, end, unwound);
    if (!frame.metrics) return;
    const value = this.functions.get(frame.name);
    value.count++;
    value.unwinds += Number(unwound);
    try {
      this.calls.add(1, value.attributes);
      this.duration.record(Number(end - frame.start) / 1e9, value.attributes);
      if (unwound) this.unwinds.add(1, value.attributes);
    } catch { this.losses.invalid++; }
  }
  async bindControl() {
    const filename = this.plan.runtime.control_socket;
    if (!filename) return;
    const parent = path.dirname(filename);
    const metadata = fs.lstatSync(parent);
    if (parent === '.' || !metadata.isDirectory() || metadata.uid !== process.getuid() || metadata.mode & 0o077) throw new Error('control directory must be owned by this user with mode 0700');
    const server = net.createServer(connection => {
      this.connections.add(connection);
      connection.once('close', () => this.connections.delete(connection));
      if (this.closed) { connection.destroy(); return; }
      connection.unref();
      connection.setTimeout(200, () => connection.end('{"error":"invalid or incomplete control request"}\n'));
      let request = '';
      let finished = false;
      connection.on('error', () => {});
      connection.on('data', data => {
        if (finished) return;
        request += data.toString();
        if (!request.includes('\n') && request.length <= 16) return;
        finished = true;
        const command = request.trimEnd();
        if (request.length > 17 || !['status', 'enable', 'disable'].includes(command)) { connection.end('{"error":"invalid or incomplete control request"}\n'); return; }
        this.promiseObserver?.flush();
        if (command !== 'status') this.enabled = command === 'enable';
        connection.end(JSON.stringify({ schema_version: 1, pid: process.pid, metrics_enabled: this.enabled, function_calls: [...this.functions.values()].reduce((sum, value) => sum + value.count, 0) }) + '\n');
      });
    });
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(filename, resolve); });
    this.socketInode = fs.lstatSync(filename).ino;
    fs.chmodSync(filename, 0o600);
    server.unref();
    this.server = server;
  }
  report() {
    this.promiseObserver?.flush();
    const result = { schema_version: 1, language: this.plan.language, pid: process.pid, export_finished: this.exportFinished, function_calls: [...this.functions.values()].reduce((sum, value) => sum + value.count, 0), functions: Object.fromEntries([...this.functions].map(([name, value]) => [name, { count: value.count, unwinds: value.unwinds }])), losses: { ...this.losses, incomplete: this.losses.incomplete + this.incomplete() }, export_loss: this.exportLoss };
    if (this.traces) result.traces = this.traces.report();
    if (process.env.OTELC_REPORT_PATH) fs.writeFileSync(process.env.OTELC_REPORT_PATH, JSON.stringify(result, null, 2) + '\n');
    return result;
  }
  incomplete() { return [...this.pending.values()].filter(frame => frame.metrics || frame.trace?.sampled).length; }
  async close() {
    if (this.closed) return this.report();
    this.promiseObserver?.close();
    this.closed = true;
    this.shutdownDeadline = process.hrtime.bigint() + BigInt(this.plan.runtime.shutdown_timeout_ms) * 1000000n;
    this.losses.incomplete += this.incomplete();
    this.pending.clear();
    this.current = null;
    this.traces?.shutdownPending();
    let timer;
    try {
      const deadline = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('shutdown deadline')), this.plan.runtime.shutdown_timeout_ms); });
      const controlClosed = this.server ? new Promise(resolve => this.server.close(resolve)) : Promise.resolve();
      for (const connection of this.connections) connection.destroy();
      const stopped = Promise.all([controlClosed, (async () => {
        // Finish trace transport while the metric reader can still collect its loss counter.
        if (this.traces) { await this.provider.forceFlush(); await this.traces.flush(); }
        await this.provider.shutdown();
        await this.traces?.flush();
      })()]).then(() => {
        if (process.hrtime.bigint() > this.shutdownDeadline) throw new Error('shutdown deadline');
        this.exportFinished = true;
      });
      await Promise.race([stopped, deadline]);
    } catch { this.exportLoss++; await this.exporter.shutdown(); }
    finally {
      clearTimeout(timer);
      await this.traces?.close();
      const filename = this.plan.runtime.control_socket;
      if (this.server && fs.existsSync(filename) && fs.lstatSync(filename).ino === this.socketInode) fs.unlinkSync(filename);
    }
    return this.report();
  }
}
