import { Worker } from 'node:worker_threads';
import { ExportResultCode } from '@opentelemetry/core';
import type { ExportResult } from '@opentelemetry/core';
import type { PushMetricExporter, ResourceMetrics } from '@opentelemetry/sdk-metrics';
import type { ReadableSpan, SpanExporter } from '@opentelemetry/sdk-trace';
import { metricToWire, spanToWire } from './export-wire.ts';
import type { ExportWorkerRequestType, IExportWorkerConfig, IExportWorkerReply, SignalType } from './export-worker-protocol.ts';

type RequestBodyType = ExportWorkerRequestType extends infer Request
  ? Request extends ExportWorkerRequestType ? Omit<Request, 'id'> : never : never;

class ExportWorker {
  readonly #worker: Worker;
  readonly #pending = new Map<number, (result: ExportResult) => void>();
  readonly #shutdown = new Map<SignalType, Promise<void>>();
  #next = 1;
  #failed = false;
  #exited = false;

  constructor(config: IExportWorkerConfig, onFailure: () => void) {
    const environment = Object.fromEntries(Object.entries(process.env).filter(([name]) =>
      name !== 'NODE_OPTIONS' && !name.startsWith('OTEL_') && !name.startsWith('OTELC_')));
    this.#worker = new Worker(new URL('./otlp-export-worker.ts', import.meta.url),
      { workerData: config, execArgv: [], env: environment, name: 'otelc-export' });
    this.#worker.on('message', (reply: IExportWorkerReply) => {
      const callback = this.#pending.get(reply.id);
      if (!callback) return;
      this.#pending.delete(reply.id);
      if (!this.#pending.size) this.#worker.unref();
      callback({ code: reply.code });
    });
    this.#worker.on('error', () => { this.#failed = true; });
    this.#worker.on('exit', code => {
      this.#exited = true;
      if (code !== 0 || this.#failed || this.#shutdown.size !== 2 || this.#pending.size) onFailure();
      const pending = [...this.#pending.values()];
      this.#pending.clear();
      for (const callback of pending) callback({ code: ExportResultCode.FAILED });
    });
    this.#worker.unref();
  }

  send(request: RequestBodyType, callback: (result: ExportResult) => void): void {
    if (this.#failed || this.#exited || this.#pending.size >= 128 || this.#next >= Number.MAX_SAFE_INTEGER) {
      callback({ code: ExportResultCode.FAILED });
      return;
    }
    const id = this.#next++;
    this.#pending.set(id, callback);
    this.#worker.ref();
    try { this.#worker.postMessage({ id, ...request }); }
    catch {
      this.#pending.delete(id);
      if (!this.#pending.size) this.#worker.unref();
      callback({ code: ExportResultCode.FAILED });
    }
  }

  command(signal: SignalType, operation: 'flush' | 'shutdown'): Promise<void> {
    return new Promise((resolve, reject) => this.send({ operation, signal }, result => {
      if (result.code === ExportResultCode.SUCCESS) resolve();
      else reject(new Error('owned OTLP export worker operation failed'));
    }));
  }

  shutdown(signal: SignalType): Promise<void> {
    let pending = this.#shutdown.get(signal);
    if (!pending) {
      pending = this.command(signal, 'shutdown');
      this.#shutdown.set(signal, pending);
    }
    return pending;
  }
}

/** Keep official OTLP protobuf/HTTP/retries in a worker with independent JS global state. */
export function createWorkerExporters(config: IExportWorkerConfig, onFailure: () => void): {
  metric: PushMetricExporter; trace: SpanExporter;
} {
  const worker = new ExportWorker(config, onFailure);
  return {
    metric: {
      export(data: ResourceMetrics, callback: (result: ExportResult) => void) {
        worker.send({ operation: 'metrics', data: metricToWire(data) }, callback);
      },
      forceFlush: () => worker.command('metrics', 'flush'),
      shutdown: () => worker.shutdown('metrics'),
    },
    trace: {
      export(data: ReadableSpan[], callback: (result: ExportResult) => void) {
        worker.send({ operation: 'traces', data: data.map(spanToWire) }, callback);
      },
      forceFlush: () => worker.command('traces', 'flush'),
      shutdown: () => worker.shutdown('traces'),
    },
  };
}
