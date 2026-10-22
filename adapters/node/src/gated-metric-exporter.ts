import { ExportResultCode } from '@opentelemetry/core';
import type { ExportResult } from '@opentelemetry/core';
import { AggregationTemporality, AggregationType } from '@opentelemetry/sdk-metrics';
import type { AggregationOption, InstrumentType, PushMetricExporter, ResourceMetrics } from '@opentelemetry/sdk-metrics';
import type { MetricControls } from './metric-controls.ts';
import { ExportAccounting } from './export-accounting.ts';

export interface IMetricStatus {
  schema_version: 1;
  pid: number;
  collection: boolean;
  emission: boolean;
  in_flight_exports: number;
  effective_emission_off: boolean;
  export_started: number;
  export_succeeded: number;
  export_failed: number;
}

/** Gate future exports without cancelling or misreporting requests already in flight. */
export class GatedMetricExporter implements PushMetricExporter {
  readonly #delegate: PushMetricExporter;
  readonly #controls: MetricControls;
  readonly #accounting = new ExportAccounting();
  #closed = false;
  #shutdown: Promise<void> | undefined;

  constructor(delegate: PushMetricExporter, controls: MetricControls) {
    this.#delegate = delegate;
    this.#controls = controls;
  }

  /** Expose raw exporter acknowledgements, not downstream receipt claims. */
  status(): IMetricStatus {
    const counts = this.#accounting.snapshot();
    return { schema_version: 1, pid: process.pid, collection: this.#controls.collection,
      emission: this.#controls.emission, in_flight_exports: counts.pending,
      effective_emission_off: !this.#controls.emission && counts.pending === 0,
      export_started: counts.started, export_succeeded: counts.succeeded, export_failed: counts.failed };
  }

  export(metrics: ResourceMetrics, callback: (result: ExportResult) => void): void {
    if (this.#closed) {
      callback({ code: ExportResultCode.FAILED, error: new Error('metric exporter is closed') });
      return;
    }
    // A disabled meter must not emit its cached snapshot when emission alone is on.
    if (!this.#controls.collection || !this.#controls.emission) {
      callback({ code: ExportResultCode.SUCCESS });
      return;
    }
    this.#accounting.export(complete => this.#delegate.export(metrics, complete), callback, 1);
  }

  selectAggregationTemporality(): AggregationTemporality { return AggregationTemporality.CUMULATIVE; }

  selectAggregation(instrumentType: InstrumentType): AggregationOption {
    return this.#delegate.selectAggregation?.(instrumentType) ?? { type: AggregationType.DEFAULT };
  }

  forceFlush(): Promise<void> { return this.#delegate.forceFlush(); }

  shutdown(): Promise<void> {
    this.#closed = true;
    this.#shutdown ??= this.#delegate.shutdown();
    return this.#shutdown;
  }
}
