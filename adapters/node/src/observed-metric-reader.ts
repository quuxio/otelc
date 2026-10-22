import { PeriodicExportingMetricReader } from '@opentelemetry/sdk-metrics';
import type { CollectionResult, MetricCollectOptions } from '@opentelemetry/sdk-metrics';

export interface ICollectionCounts { started: number; failed: number; pending: number }

/** Observe public SDK collection results before the periodic reader can absorb errors. */
export class ObservedMetricReader extends PeriodicExportingMetricReader {
  readonly #counts: ICollectionCounts = { started: 0, failed: 0, pending: 0 };

  status(): ICollectionCounts { return { ...this.#counts }; }

  async collect(options?: MetricCollectOptions): Promise<CollectionResult> {
    this.#counts.started++;
    this.#counts.pending++;
    try {
      const result = await super.collect(options);
      if (result.errors.length) this.#counts.failed++;
      return result;
    } catch (error) {
      this.#counts.failed++;
      throw error;
    } finally { this.#counts.pending--; }
  }
}
