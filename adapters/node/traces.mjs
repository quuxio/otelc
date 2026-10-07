import { ROOT_CONTEXT, trace, TraceFlags, SpanStatusCode, isSpanContextValid } from '@opentelemetry/api';
import { TracerProvider, ParentBasedSampler, TraceIdRatioBasedSampler } from '@opentelemetry/sdk-trace';
import { TraceExporter, MAX_BYTES } from './trace-exporter.mjs';

export const SUPPRESSED = Object.freeze({ sampled: false });
const MAX_RECORDS = 1048576;
const hrTime = nanos => [Number(nanos / 1000000000n), Number(nanos % 1000000000n)];
// Only SDK spans and scalar metadata are retained; complete trees export together.
export class TraceStore {
  constructor(runtime, resource) {
    this.runtime = runtime;
    const p = runtime.plan;
    try {
      const url = new URL(p.trace_export.endpoint);
      const integer = (value, maximum) => Number.isSafeInteger(value) && value > 0 && value <= maximum;
      if (p.traces.enabled !== true || typeof p.traces.root_sample_ratio !== 'number' || !Number.isFinite(p.traces.root_sample_ratio) || p.traces.root_sample_ratio < 0 || p.traces.root_sample_ratio > 1
          || !integer(p.traces.max_active_traces, 65536) || !integer(p.traces.max_spans_per_trace, 65536)
          || p.traces.max_active_traces * p.traces.max_spans_per_trace > MAX_RECORDS || !integer(p.export.max_queued_batches, 64)
          || !integer(p.trace_export.timeout_ms, 60000) || p.trace_export.protocol !== 'http/protobuf'
          || url.username || url.password || url.search || url.hash || !(url.protocol === 'https:' || url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))) throw new Error();
    } catch { throw new Error('invalid resolved JavaScript trace settings'); }
    this.provider = new TracerProvider({ resource, sampler: new ParentBasedSampler({ root: new TraceIdRatioBasedSampler(p.traces.root_sample_ratio) }),
      spanLimits: { attributeCountLimit: 1, attributeValueLengthLimit: 1024, eventCountLimit: 0, linkCountLimit: 0 } });
    this.tracer = this.provider.getTracer('quux.otelc', '0.1.0');
    this.exporter = new TraceExporter(runtime);
    this.roots = new Map(); this.ready = []; this.retained = 0;
    this.losses = {}; this.completed = 0; this.sampledOut = 0; this.activeFlush = null;
  }
  lose(reason) { this.losses[reason] = (this.losses[reason] ?? 0) + 1; }
  reject(parent, reason) {
    if (parent === null) this.lose(reason);
    else if (parent?.sampled) {
      const tree = this.roots.get(parent.context.traceId);
      if (tree && !tree.invalid) { tree.invalid = true; this.retained -= tree.nodes.size; tree.nodes.clear(); this.lose(reason); }
    }
    return SUPPRESSED;
  }
  begin(parent, name, started) {
    let tree, context = ROOT_CONTEXT, epoch = BigInt(Date.now()) * 1000000n;
    if (parent !== null) {
      if (!parent?.sampled) return SUPPRESSED;
      tree = this.roots.get(parent.context.traceId);
      if (!tree || tree.invalid) return SUPPRESSED;
      if (tree.nodes.size >= this.runtime.plan.traces.max_spans_per_trace || this.retained >= MAX_RECORDS) return this.reject(parent, 'span_capacity');
      context = trace.setSpanContext(ROOT_CONTEXT, parent.context);
      epoch = tree.epoch + (started > tree.origin ? started - tree.origin : 0n);
    }
    const span = this.tracer.startSpan(name, { startTime: hrTime(epoch), attributes: { 'code.function.name': name } }, context);
    const identity = span.spanContext();
    if (!isSpanContextValid(identity)) return this.reject(parent, 'invalid');
    if (!(identity.traceFlags & TraceFlags.SAMPLED)) { this.sampledOut++; return SUPPRESSED; }
    if (parent === null) {
      if (this.roots.size >= this.runtime.plan.traces.max_active_traces || this.retained >= MAX_RECORDS) return this.reject(null, 'trace_capacity');
      if (this.roots.has(identity.traceId)) return this.reject(null, 'invalid');
      tree = { root: identity.spanId, origin: started, epoch, nodes: new Map(), active: 0, invalid: false, closed: false, bytes: 0 };
      this.roots.set(identity.traceId, tree);
    }
    if (tree.nodes.has(identity.spanId)) return this.reject(parent, 'invalid');
    tree.nodes.set(identity.spanId, span); tree.active++; this.retained++;
    tree.bytes += 2 * Buffer.byteLength(name) + 256;
    return { sampled: true, context: identity, started, finished: false };
  }
  finish(identity, ended, escaped) {
    if (!identity?.sampled || identity.finished) return;
    identity.finished = true;
    const tree = this.roots.get(identity.context.traceId);
    if (!tree) return;
    if (!tree.invalid) {
      const span = tree.nodes.get(identity.context.spanId);
      if (escaped) span.setStatus({ code: SpanStatusCode.ERROR, message: 'escaping unwind' });
      const clamped = ended < identity.started ? identity.started : ended;
      span.end(hrTime(tree.epoch + (clamped > tree.origin ? clamped - tree.origin : 0n)));
    }
    tree.active--;
    if (identity.context.spanId === tree.root) tree.closed = true;
    if (tree.active || !tree.closed) return;
    this.roots.delete(identity.context.traceId);
    if (tree.invalid) return;
    if (this.ready.length >= this.runtime.plan.export.max_queued_batches) { this.retained -= tree.nodes.size; this.lose('queue_capacity'); }
    else { this.completed++; this.ready.push(tree); }
  }
  shutdownPending() {
    for (const tree of this.roots.values()) { this.retained -= tree.nodes.size; if (!tree.invalid) this.lose('incomplete'); }
    this.roots.clear();
  }
  report() { return { completed_trees: this.completed, sampled_out_roots: this.sampledOut, active_trees: this.roots.size, queued_trees: this.ready.length, losses: { ...this.losses } }; }
  flush() {
    if (this.activeFlush) return this.activeFlush;
    const deadline = process.hrtime.bigint() + BigInt(this.runtime.plan.trace_export.timeout_ms) * 1000000n;
    const drain = async () => {
      let success = true;
      for (let remaining = this.runtime.plan.export.max_queued_batches; remaining && this.ready.length; remaining--) {
        const tree = this.ready.shift(); this.retained -= tree.nodes.size;
        if (tree.bytes > MAX_BYTES) { this.lose('batch_bytes'); continue; }
        const accepted = await this.exporter.export([...tree.nodes.values()], deadline < this.runtime.shutdownDeadline ? deadline : this.runtime.shutdownDeadline);
        if (!accepted) { success = false; this.runtime.exportLoss++; }
      }
      return success;
    };
    this.activeFlush = drain().finally(() => { this.activeFlush = null; });
    return this.activeFlush;
  }
  async close() { this.exporter.close(); await this.provider.shutdown(); }
}
