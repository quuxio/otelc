import type { ExportResultCode } from '@opentelemetry/core';
import type { IMetricWire, ISpanWire } from './export-wire.ts';

export interface IExportWorkerConfig { endpoint: string; timeoutMs: number }
export type SignalType = 'metrics' | 'traces';
export type ExportWorkerRequestType =
  | { id: number; operation: 'metrics'; data: IMetricWire }
  | { id: number; operation: 'traces'; data: ISpanWire[] }
  | { id: number; operation: 'flush' | 'shutdown'; signal: SignalType };
export interface IExportWorkerReply { id: number; code: ExportResultCode }
