import { context, trace } from '@opentelemetry/api';
import type { Attributes, Context, Counter, Gauge, Histogram, MeterOptions, MetricOptions } from '@opentelemetry/api';
import { contextToWire } from './export-wire.ts';
import type { MetricControls } from './metric-controls.ts';
import type { SourceSdkRecordType } from './source-sdk-protocol.ts';

/** Probe-owned primitive measurements; the child official SDK owns all aggregation. */
export class SourceMetrics {
  readonly #scope: string;
  readonly #controls: MetricControls;
  readonly #send: (record: SourceSdkRecordType) => boolean;
  readonly #version: string;
  readonly #options?: MeterOptions;

  constructor(scope: string, controls: MetricControls, send: (record: SourceSdkRecordType) => boolean,
    version = '0.2.0', options?: MeterOptions) {
    this.#scope = scope; this.#controls = controls; this.#send = send;
    this.#version = version; this.#options = options;
  }

  #record(instrument: 'counter' | 'histogram' | 'gauge', name: string, options: MetricOptions | undefined,
    value: number, attributes?: Attributes, active?: Context): void {
    if (!this.#controls.collection) return;
    const span = trace.getSpanContext(active ?? context.active());
    this.#send({ kind: 'metric', scope: this.#scope, version: this.#version, schemaUrl: this.#options?.schemaUrl,
      instrument, name, options, value, attributes, context: span ? contextToWire(span) : undefined });
  }

  counter(name: string, options?: MetricOptions): Counter {
    return { add: (value, attributes, active?: Context) => this.#record('counter', name, options, value, attributes, active) };
  }

  histogram(name: string, options?: MetricOptions): Histogram {
    return { record: (value, attributes, active?: Context) => this.#record('histogram', name, options, value, attributes, active) };
  }

  gauge(name: string, options?: MetricOptions): Gauge {
    return { record: (value, attributes, active?: Context) => this.#record('gauge', name, options, value, attributes, active) };
  }
}
