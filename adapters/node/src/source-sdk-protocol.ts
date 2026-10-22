import type { Attributes, MetricOptions } from '@opentelemetry/api';
import type { ISpanWire } from './export-wire.ts';
import type { IControlDescriptor } from './local-metric-control.ts';
import type { SourceRuntime } from './source-runtime.ts';

export type SourceCheckpointType = ReturnType<SourceRuntime['snapshot']>;

export interface ISourceMetric {
  kind: 'metric'; scope: string; instrument: 'counter' | 'histogram' | 'gauge';
  name: string; options?: MetricOptions; value: number; attributes?: Attributes;
}

export type SourceSdkRecordType = ISourceMetric
  | { kind: 'span'; span: ISpanWire }
  | { kind: 'controls'; collection: boolean; emission: boolean }
  | { kind: 'request'; id: number; command: 'status' | 'flush' | 'shutdown' }
  | { kind: 'checkpoint'; status: SourceCheckpointType }
  | { kind: 'control-descriptor'; descriptor: IControlDescriptor };

export interface ISourceSdkReply { id: number; ok: boolean; status: object }
