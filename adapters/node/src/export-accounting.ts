import { ExportResultCode } from '@opentelemetry/core';
import type { ExportResult } from '@opentelemetry/core';

export interface IExportCounts {
  started: number;
  succeeded: number;
  failed: number;
  pending: number;
  exported_items: number;
}

/** Count actual SDK exporter acknowledgements once, including synchronous failures. */
export class ExportAccounting {
  readonly #counts: IExportCounts = { started: 0, succeeded: 0, failed: 0, pending: 0, exported_items: 0 };

  snapshot(): IExportCounts { return { ...this.#counts }; }

  /** Translate an exporter contract failure without leaving a pending request stuck. */
  export(send: (done: (result: ExportResult) => void) => void,
         callback: (result: ExportResult) => void, items: number): void {
    this.#counts.started++;
    this.#counts.pending++;
    let completed = false;
    const complete = (result: ExportResult): void => {
      if (completed) return;
      completed = true;
      this.#counts.pending--;
      if (result.code === ExportResultCode.SUCCESS) {
        this.#counts.succeeded++;
        this.#counts.exported_items += items;
      } else this.#counts.failed++;
      callback(result);
    };
    try { send(complete); }
    catch { complete({ code: ExportResultCode.FAILED, error: new Error('SDK exporter threw') }); }
  }
}
