import http from 'node:http';
import https from 'node:https';
import { ProtobufTraceSerializer } from '@opentelemetry/otlp-transformer';
import { headers } from './exporter.mjs';

export const MAX_BYTES = 16 * 1024 * 1024;
// Encode once; retry only transient failures within the original phase deadline.
export class TraceExporter {
  constructor(runtime) {
    this.runtime = runtime;
    this.endpoint = new URL(runtime.plan.trace_export.endpoint);
    this.headers = headers(process.env, 'OTEL_EXPORTER_OTLP_TRACES_HEADERS');
    this.requests = new Set();
    this.closed = false;
  }
  async export(spans, deadline) {
    let payload;
    try { payload = ProtobufTraceSerializer.serializeRequest(spans); }
    catch { return false; }
    if (payload.length > MAX_BYTES) return false;
    for (let remaining = 3; remaining > 0; remaining--) {
      const result = await this.attempt(payload, deadline);
      if (result === 'accepted') return true;
      if (result !== 'transient') return false;
    }
    return false;
  }
  attempt(payload, deadline) {
    const budget = Number(deadline - process.hrtime.bigint()) / 1e6;
    if (this.closed || budget <= 0) return Promise.resolve('rejected');
    return new Promise(resolve => {
      let completed = false, timer, request;
      const finish = result => {
        if (completed) return;
        completed = true;
        clearTimeout(timer); this.requests.delete(request); resolve(result);
      };
      try {
        request = (this.endpoint.protocol === 'https:' ? https : http).request(this.endpoint, {
          method: 'POST', headers: { ...this.headers, 'Content-Type': 'application/x-protobuf', 'Content-Length': payload.length }
        }, response => {
          if (response.statusCode !== 200) {
            finish([429, 502, 503, 504].includes(response.statusCode) ? 'transient' : 'rejected'); response.destroy(); request.destroy(); return;
          }
          const chunks = []; let size = 0;
          response.on('data', chunk => {
            size += chunk.length;
            if (size > 65536) { finish('rejected'); response.destroy(); request.destroy(); }
            else chunks.push(chunk);
          });
          response.on('error', () => finish('transient'));
          response.on('end', () => {
            try {
              const result = ProtobufTraceSerializer.deserializeResponse(Buffer.concat(chunks));
              finish(!result.partialSuccess || BigInt(result.partialSuccess.rejectedSpans ?? 0) === 0n ? 'accepted' : 'rejected');
            } catch { finish('rejected'); }
          });
        });
        this.requests.add(request);
        request.on('error', () => finish('transient'));
        timer = setTimeout(() => { finish('rejected'); request.destroy(); }, Math.min(budget, 60000));
        request.end(payload);
      } catch { finish('rejected'); request?.destroy(); }
    });
  }
  close() { this.closed = true; for (const request of this.requests) request.destroy(); }
}
