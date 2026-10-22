import { createTraceState } from '@opentelemetry/api';
import type { Attributes, Link, SpanContext } from '@opentelemetry/api';
import { resourceFromAttributes } from '@opentelemetry/resources';
import type { Resource } from '@opentelemetry/resources';
import type { ResourceMetrics } from '@opentelemetry/sdk-metrics';
import type { ReadableSpan } from '@opentelemetry/sdk-trace';

interface IResourceWire { attributes: Attributes; schemaUrl?: string }
interface ISpanContextWire extends Omit<SpanContext, 'traceState'> { traceState?: string }
interface ILinkWire extends Omit<Link, 'context'> { context: ISpanContextWire }
export interface IMetricWire extends Omit<ResourceMetrics, 'resource'> { resource: IResourceWire }
export interface ISpanWire extends Omit<ReadableSpan, 'spanContext' | 'parentSpanContext' | 'links' | 'resource'> {
  spanContext: ISpanContextWire;
  parentSpanContext?: ISpanContextWire;
  links: ILinkWire[];
  resource: IResourceWire;
}

function contextToWire(value: SpanContext): ISpanContextWire {
  return { traceId: value.traceId, spanId: value.spanId, traceFlags: value.traceFlags,
    isRemote: value.isRemote, traceState: value.traceState?.serialize() };
}

function contextFromWire(value: ISpanContextWire): SpanContext {
  return { ...value, traceState: value.traceState === undefined ? undefined : createTraceState(value.traceState) };
}

function resourceToWire(value: Resource): IResourceWire {
  return { attributes: value.attributes, schemaUrl: value.schemaUrl };
}

function resourceFromWire(value: IResourceWire): Resource {
  return resourceFromAttributes(value.attributes, { schemaUrl: value.schemaUrl });
}

/** Snapshot public SDK data; no SDK private fields or application objects cross the port. */
export function metricToWire(value: ResourceMetrics): IMetricWire {
  return { resource: resourceToWire(value.resource), scopeMetrics: value.scopeMetrics };
}

/** Recreate only the official resource object; SDK aggregated points remain unchanged. */
export function metricFromWire(value: IMetricWire): ResourceMetrics {
  return { ...value, resource: resourceFromWire(value.resource) };
}

/** Copy the public ended-span interface, including parentage, links and drop counts. */
export function spanToWire(value: ReadableSpan): ISpanWire {
  return { name: value.name, kind: value.kind, spanContext: contextToWire(value.spanContext()),
    parentSpanContext: value.parentSpanContext ? contextToWire(value.parentSpanContext) : undefined,
    startTime: value.startTime, endTime: value.endTime, duration: value.duration,
    status: value.status, attributes: value.attributes, ended: value.ended,
    links: value.links.map(link => ({ ...link, context: contextToWire(link.context) })),
    events: value.events, instrumentationScope: value.instrumentationScope,
    resource: resourceToWire(value.resource), droppedAttributesCount: value.droppedAttributesCount,
    droppedEventsCount: value.droppedEventsCount, droppedLinksCount: value.droppedLinksCount };
}

/** Restore the public span interface for the official exporter in its isolated worker. */
export function spanFromWire(value: ISpanWire): ReadableSpan {
  const { spanContext, parentSpanContext, links, resource, ...data } = value;
  return { ...data, spanContext: () => contextFromWire(spanContext),
    parentSpanContext: parentSpanContext ? contextFromWire(parentSpanContext) : undefined,
    links: links.map(link => ({ ...link, context: contextFromWire(link.context) })),
    resource: resourceFromWire(resource) };
}
