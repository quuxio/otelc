import { OTLPMetricExporter } from '@opentelemetry/exporter-metrics-otlp-proto';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-proto';
import { defaultResource, resourceFromAttributes } from '@opentelemetry/resources';
import { AggregationType, MeterProvider } from '@opentelemetry/sdk-metrics';
import {
  AlwaysOffSampler, BatchSpanProcessor, ParentBasedSampler, TraceIdRatioBasedSampler, TracerProvider,
} from '@opentelemetry/sdk-trace';
import { GatedMeterProvider } from './gated-meter-provider.ts';
import { GatedMetricExporter } from './gated-metric-exporter.ts';
import type { IMetricStatus } from './gated-metric-exporter.ts';
import { MetricControls } from './metric-controls.ts';
import { ObservedSpanExporter } from './observed-span-exporter.ts';
import type { IExportCounts } from './export-accounting.ts';
import { ObservedMetricReader } from './observed-metric-reader.ts';
import type { ICollectionCounts } from './observed-metric-reader.ts';

/** Subset consumed from the single, already validated schema-3 common policy. */
export interface ISdkPolicy {
  resource: { service_name: string };
  metrics: { collection: boolean; emission: boolean; histogram_boundaries_seconds: number[] };
  traces: { enabled: boolean; sample_ratio: number };
  export: { endpoint: string; interval_ms: number; timeout_ms: number; shutdown_timeout_ms: number };
}

export interface ISdkStatus extends IMetricStatus {
  phase: 'running' | 'closing' | 'closed' | 'failed';
  reader_timeout_ms: number;
  traces: IExportCounts;
  metric_collection: ICollectionCounts;
}

async function requireSuccess(operations: Promise<unknown>[]): Promise<void> {
  const results = await Promise.allSettled(operations);
  if (results.some(result => result.status === 'rejected')) {
    // SDK failures can contain application callback errors. Do not format those objects.
    throw new Error('owned OpenTelemetry SDK operation failed');
  }
}

async function deadline(operation: Promise<void>, timeoutMs: number): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([operation, new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error('owned SDK shutdown deadline exceeded')), timeoutMs);
    })]);
  } finally { clearTimeout(timer); }
}

/** Official SDKs/exporters with metric-only controls; this does not install source probes. */
export class OwnedSdkPipeline {
  readonly controls: MetricControls;
  readonly meterProvider: GatedMeterProvider;
  readonly tracerProvider: TracerProvider;
  readonly #metrics: MeterProvider;
  readonly #exporter: GatedMetricExporter;
  readonly #traceExporter: ObservedSpanExporter;
  readonly #reader: ObservedMetricReader;
  readonly #readerTimeoutMs: number;
  readonly #shutdownTimeoutMs: number;
  #phase: ISdkStatus['phase'] = 'running';
  #shutdown: Promise<void> | undefined;

  constructor(policy: ISdkPolicy) {
    this.controls = new MetricControls(policy.metrics.collection, policy.metrics.emission);
    const resource = defaultResource().merge(resourceFromAttributes({ 'service.name': policy.resource.service_name,
      'service.instance.id': String(process.pid) }));
    const endpoint = policy.export.endpoint.replace(/\/$/, '');
    this.#exporter = new GatedMetricExporter(new OTLPMetricExporter({ url: endpoint + '/v1/metrics',
      timeoutMillis: policy.export.timeout_ms }), this.controls);
    // The pinned periodic reader requires collection timeout <= interval. The HTTP
    // transport retains the configured timeout; report the reader's shorter budget.
    this.#readerTimeoutMs = Math.min(policy.export.interval_ms, policy.export.timeout_ms);
    this.#shutdownTimeoutMs = policy.export.shutdown_timeout_ms;
    this.#reader = new ObservedMetricReader({ exporter: this.#exporter,
      exportIntervalMillis: policy.export.interval_ms, exportTimeoutMillis: this.#readerTimeoutMs,
      maxExportBatchSize: 512 });
    this.#metrics = new MeterProvider({ resource, readers: [this.#reader], views:
      ['otelc.function.duration', 'otelc.lifetime.duration'].map(instrumentName => ({ instrumentName,
        aggregation: { type: AggregationType.EXPLICIT_BUCKET_HISTOGRAM,
          options: { boundaries: policy.metrics.histogram_boundaries_seconds } } })) });
    this.meterProvider = new GatedMeterProvider(this.#metrics, this.controls);
    const sampler = policy.traces.enabled
      ? new ParentBasedSampler({ root: new TraceIdRatioBasedSampler(policy.traces.sample_ratio) })
      : new AlwaysOffSampler();
    this.#traceExporter = new ObservedSpanExporter(new OTLPTraceExporter({ url: endpoint + '/v1/traces',
      timeoutMillis: policy.export.timeout_ms }));
    this.tracerProvider = new TracerProvider({ resource, sampler, meterProvider: this.meterProvider,
      spanProcessors: [new BatchSpanProcessor({ exporter: this.#traceExporter, scheduledDelayMillis: policy.export.interval_ms,
        exportTimeoutMillis: policy.export.timeout_ms, maxQueueSize: 2048, maxExportBatchSize: 512,
        selfObsMeterProvider: this.meterProvider })] });
  }

  /** Report acknowledgements and phase; neither establishes downstream receipt. */
  status(): ISdkStatus {
    return { ...this.#exporter.status(), phase: this.#phase, reader_timeout_ms: this.#readerTimeoutMs,
      traces: this.#traceExporter.status(), metric_collection: this.#reader.status() };
  }

  /** Flush both independent signals; a rejected SDK operation cannot become success. */
  async flush(): Promise<void> {
    if (this.#phase !== 'running') throw new Error('owned SDK pipeline is not running');
    const before = this.status();
    await requireSuccess([this.#metrics.forceFlush({ timeoutMillis: this.#readerTimeoutMs }),
      this.tracerProvider.forceFlush({ timeoutMillis: this.#readerTimeoutMs })]);
    this.#verifyExports(before, false);
  }

  #verifyExports(before: ISdkStatus, requireIdle: boolean): void {
    const after = this.status();
    // Each signal's forceFlush has its own SDK barrier. A periodic export can
    // start after one barrier while the other signal is still flushing. Only
    // shutdown stops the producers and can require global idle afterwards.
    if (after.export_failed !== before.export_failed || after.traces.failed !== before.traces.failed
        || after.metric_collection.failed !== before.metric_collection.failed
        || requireIdle && (after.in_flight_exports || after.traces.pending || after.metric_collection.pending)) {
      throw new Error('owned OpenTelemetry SDK collection/export failed or remains pending');
    }
  }

  /** Bound the caller's wait and retain failure rather than claiming a completed drain. */
  shutdown(): Promise<void> {
    this.#shutdown ??= this.#stop();
    return this.#shutdown;
  }

  async #stop(): Promise<void> {
    this.#phase = 'closing';
    const before = this.status();
    try {
      await deadline(requireSuccess([this.#metrics.shutdown({ timeoutMillis: this.#shutdownTimeoutMs }),
        this.tracerProvider.shutdown()]), this.#shutdownTimeoutMs);
      this.#verifyExports(before, true);
      this.#phase = 'closed';
    } catch (error) { this.#phase = 'failed'; throw error; }
  }
}
