import type { Counter, Gauge, Histogram, MetricOptions } from '@opentelemetry/api';
import type { MetricControls } from './metric-controls.ts';
import type { SourceSdkRecordType } from './source-sdk-protocol.ts';

/** Probe-owned primitive measurements; the child official SDK owns all aggregation. */
export class SourceMetrics {
  readonly #scope: string;
  readonly #controls: MetricControls;
  readonly #send: (record: SourceSdkRecordType) => boolean;

  constructor(scope: string, controls: MetricControls, send: (record: SourceSdkRecordType) => boolean) {
    this.#scope = scope; this.#controls = controls; this.#send = send;
  }

  counter(name: string, options?: MetricOptions): Counter {
    return { add: (value, attributes) => {
      if (this.#controls.collection) this.#send({ kind: 'metric', scope: this.#scope,
        instrument: 'counter', name, options, value, attributes });
    } };
  }

  histogram(name: string, options?: MetricOptions): Histogram {
    return { record: (value, attributes) => {
      if (this.#controls.collection) this.#send({ kind: 'metric', scope: this.#scope,
        instrument: 'histogram', name, options, value, attributes });
    } };
  }

  gauge(name: string, options?: MetricOptions): Gauge {
    return { record: (value, attributes) => {
      if (this.#controls.collection) this.#send({ kind: 'metric', scope: this.#scope,
        instrument: 'gauge', name, options, value, attributes });
    } };
  }
}
