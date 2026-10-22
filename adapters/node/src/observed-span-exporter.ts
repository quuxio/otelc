import type { ExportResult } from '@opentelemetry/core';
import type { ReadableSpan, SpanExporter } from '@opentelemetry/sdk-trace';
import { ExportAccounting } from './export-accounting.ts';
import type { IExportCounts } from './export-accounting.ts';

/** Observe the official trace exporter without applying metric switches to it. */
export class ObservedSpanExporter implements SpanExporter {
  readonly #delegate: SpanExporter;
  readonly #accounting = new ExportAccounting();

  constructor(delegate: SpanExporter) { this.#delegate = delegate; }

  status(): IExportCounts { return this.#accounting.snapshot(); }

  export(spans: ReadableSpan[], callback: (result: ExportResult) => void): void {
    this.#accounting.export(complete => this.#delegate.export(spans, complete), callback, spans.length);
  }

  forceFlush(): Promise<void> { return this.#delegate.forceFlush?.() ?? Promise.resolve(); }
  shutdown(): Promise<void> { return this.#delegate.shutdown(); }
}
