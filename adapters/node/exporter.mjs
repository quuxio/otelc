// SDK protobuf encoding with bounded requests and explicit partial-success loss.
import http from 'node:http';
import https from 'node:https';
import { ProtobufMetricsSerializer } from '@opentelemetry/otlp-transformer';
import { AggregationTemporality } from '@opentelemetry/sdk-metrics';
import { ExportResultCode } from '@opentelemetry/core';

export function headers(environment = process.env) {
  const result = {};
  for (const item of (environment.OTEL_EXPORTER_OTLP_METRICS_HEADERS ?? environment.OTEL_EXPORTER_OTLP_HEADERS ?? '').split(',')) {
    if (!item.trim()) continue;
    const separator = item.indexOf('=');
    if (separator < 1) throw new Error('invalid OTLP headers');
    const name = decodeURIComponent(item.slice(0, separator).trim());
    const value = decodeURIComponent(item.slice(separator + 1).trim());
    if (/[\r\n]/.test(name + value)) throw new Error('invalid OTLP headers');
    http.validateHeaderName(name);
    http.validateHeaderValue(name, value);
    result[name] = value;
  }
  return result;
}
export class Exporter {
  constructor(runtime) {
    this.runtime = runtime;
    this.headers = headers();
    this.requests = new Set();
    this.signature = null;
  }
  selectAggregationTemporality() { return AggregationTemporality.CUMULATIVE; }
  export(data, callback) {
    if (this.requests.size >= 1) { this.runtime.exportLoss++; callback({ code: ExportResultCode.FAILED }); return; }
    const signature = JSON.stringify(data.scopeMetrics.flatMap(scope => scope.metrics
      .filter(metric => metric.descriptor.name !== 'otelc.export.dropped_batches')
      .map(metric => [metric.descriptor.name, metric.dataPoints.map(point => [point.attributes, typeof point.value === 'object' ? point.value.count : point.value])])));
    if (signature === this.signature) { callback({ code: ExportResultCode.SUCCESS }); return; }
    let payload;
    try { payload = ProtobufMetricsSerializer.serializeRequest(data); }
    catch { this.runtime.exportLoss++; callback({ code: ExportResultCode.FAILED }); return; }
    const endpoint = new URL(this.runtime.plan.metrics_endpoint);
    let completed = false;
    let timer;
    const finish = success => {
      if (completed) return;
      completed = true;
      clearTimeout(timer);
      this.requests.delete(request);
      if (success) this.signature = signature;
      if (!success) this.runtime.exportLoss++;
      callback({ code: success ? ExportResultCode.SUCCESS : ExportResultCode.FAILED });
    };
    const request = (endpoint.protocol === 'https:' ? https : http).request(endpoint, {
      method: 'POST', headers: { ...this.headers, 'Content-Type': 'application/x-protobuf', 'Content-Length': payload.length }
    }, response => {
      if (response.statusCode !== 200) { response.resume(); finish(false); return; }
      const chunks = [];
      let size = 0;
      response.on('data', chunk => {
        size += chunk.length;
        if (size > 65536) { response.destroy(); finish(false); }
        else chunks.push(chunk);
      });
      response.on('error', () => finish(false));
      response.on('end', () => {
        try {
          const result = ProtobufMetricsSerializer.deserializeResponse(Buffer.concat(chunks));
          finish(!result.partialSuccess || BigInt(result.partialSuccess.rejectedDataPoints ?? 0) === 0n);
        } catch { finish(false); }
      });
    });
    this.requests.add(request);
    request.on('error', () => finish(false));
    timer = setTimeout(() => { request.destroy(); finish(false); }, this.runtime.timeoutMs);
    request.end(payload);
  }
  async forceFlush() {}
  async shutdown() {
    for (const request of this.requests) request.destroy();
  }
}
