import { parentPort, workerData } from 'node:worker_threads';
import { ExportResultCode } from '@opentelemetry/core';
import type { ExportResult } from '@opentelemetry/core';
import { OTLPMetricExporter } from '@opentelemetry/exporter-metrics-otlp-proto';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-proto';
import { metricFromWire, spanFromWire } from './export-wire.ts';
import type { ExportWorkerRequestType, IExportWorkerConfig, IExportWorkerReply } from './export-worker-protocol.ts';

// The owning producer supplies this private worker's closed protocol and validated settings.
const config = workerData as IExportWorkerConfig;
const port = parentPort!;
const metric = new OTLPMetricExporter({ url: config.endpoint + '/v1/metrics', timeoutMillis: config.timeoutMs });
const trace = new OTLPTraceExporter({ url: config.endpoint + '/v1/traces', timeoutMillis: config.timeoutMs });
const closed = new Set<string>();

async function execute(request: ExportWorkerRequestType): Promise<ExportResult> {
  if (request.operation === 'metrics') {
    return new Promise(resolve => metric.export(metricFromWire(request.data), resolve));
  }
  if (request.operation === 'traces') {
    return new Promise(resolve => trace.export(request.data.map(spanFromWire), resolve));
  }
  const exporter = request.signal === 'metrics' ? metric : trace;
  if (request.operation === 'flush') await exporter.forceFlush();
  else { await exporter.shutdown(); closed.add(request.signal); }
  return { code: ExportResultCode.SUCCESS };
}

port.on('message', (request: ExportWorkerRequestType) => {
  void execute(request).then(result => {
    const reply: IExportWorkerReply = { id: request.id, code: result.code };
    port.postMessage(reply);
    if (closed.size === 2) port.close();
  }, () => {
    // SDK errors can contain destination details. Return only an acknowledgement code.
    const reply: IExportWorkerReply = { id: request.id, code: ExportResultCode.FAILED };
    port.postMessage(reply);
  });
});
