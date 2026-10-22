import { isSpanContextValid, ROOT_CONTEXT, ValueType } from '@opentelemetry/api';
import type { Counter, Gauge, Histogram, SpanContext, Tracer } from '@opentelemetry/api';
import type { SourceMetrics } from './source-metrics.ts';
import { epochNs, monotonicNs, sdkTime } from './source-clock.ts';
import type { ISourcePlan } from './source-plan.ts';

interface IReleaseKey { key: PropertyKey; site: number; supported: boolean; field: boolean }
interface IConstructor { name: string; resource: boolean; keys: readonly IReleaseKey[]; sites: ReadonlySet<number> }
interface ITracked {
  id: number;
  type: IConstructor;
  name: string; resource: boolean; released: boolean; metrics: boolean;
  started: bigint; epoch: bigint; parent?: SpanContext;
}

/** Bounded weak identities; observed GC and successful synchronous void release stay distinct. */
export class SourceLifetimes {
  readonly #plan: ISourcePlan;
  readonly #observing: () => boolean;
  readonly #collecting: () => boolean;
  readonly #tracer: Tracer;
  readonly #constructors = new WeakMap<object, IConstructor>();
  readonly #constructorEntries = new Set<IConstructor>();
  readonly #identities = new WeakMap<object, number>();
  readonly #instances = new Map<number, ITracked>();
  readonly #classes = new FinalizationRegistry<IConstructor>(entry => this.#constructorEntries.delete(entry));
  readonly #registry = new FinalizationRegistry<number>(token => this.#collected(token));
  readonly #admitted: Counter;
  readonly #completed: Counter;
  readonly #incomplete: Counter;
  readonly #lost: Counter;
  readonly #duration: Histogram;
  readonly #live: Gauge;
  readonly #losses: Record<string, number> = {};
  #next = 1;
  #closed = false;
  #admissions = 0;
  #gcCompleted = 0;
  #resourceCompleted = 0;
  #openResources = 0;
  #failedConstructions = 0;
  #failedReleases = 0;
  #censored = 0;
  #censoredResources = 0;

  constructor(plan: ISourcePlan, meter: SourceMetrics, tracer: Tracer,
    observing: () => boolean, collecting: () => boolean) {
    this.#plan = plan; this.#tracer = tracer; this.#observing = observing; this.#collecting = collecting;
    this.#admitted = meter.counter('otelc.lifetime.admitted', { valueType: ValueType.INT });
    this.#completed = meter.counter('otelc.lifetime.completed', { valueType: ValueType.INT });
    this.#incomplete = meter.counter('otelc.lifetime.incomplete', { valueType: ValueType.INT });
    this.#lost = meter.counter('otelc.lifetime.lost', { valueType: ValueType.INT });
    this.#duration = meter.histogram('otelc.lifetime.duration', { unit: 's' });
    this.#live = meter.gauge('otelc.lifetime.live', { valueType: ValueType.INT });
  }

  /** Cache only the producer's current weak-metadata counts in the child SDK gauge. */
  gauges(): void {
    this.#live.record(this.#closed ? this.#censored : this.#instances.size, { type: 'all', boundary: 'observed_gc' });
    this.#live.record(this.#closed ? this.#censoredResources : this.#openResources,
      { type: 'all', boundary: 'resource' });
  }

  /** Register the actual class without retaining it or adding instance/prototype fields. */
  type(constructor: object, name: string, keys: readonly IReleaseKey[]): void {
    if (this.#closed || this.#constructors.has(constructor)) return;
    if (this.#constructorEntries.size >= this.#plan.runtime.max_functions || Buffer.byteLength(name) > 1024) {
      this.lose('constructor_capacity'); return;
    }
    const methods = new Map<PropertyKey, IReleaseKey>();
    for (const key of keys) if (!key.field) methods.set(key.key, key);
    // Instance fields shadow prototype methods, regardless of declaration order.
    for (const key of keys) if (key.field) methods.set(key.key, key);
    const sites = new Set<number>();
    for (const release of this.#plan.lifetimes.release_methods) {
      const method = methods.get(release);
      if (method?.supported) sites.add(method.site);
      else if (method) this.lose('unsupported_release');
    }
    const entry: IConstructor = { name, resource: sites.size > 0, keys, sites };
    this.#constructors.set(constructor, entry); this.#constructorEntries.add(entry);
    this.#classes.register(constructor, entry, entry);
  }

  failedConstruction(constructor: object, name: string): void {
    if (!this.#closed && this.#constructors.get(constructor)?.name === name) this.#failedConstructions++;
  }

  admit(value: object, constructor: object, name: string, parent?: SpanContext): void {
    if (this.#closed || !this.#observing()) return;
    const type = this.#constructors.get(constructor);
    if (type?.name !== name) { this.lose('unsupported_constructor'); return; }
    if (this.#identities.has(value)) return;
    if (this.#instances.size >= this.#plan.lifetimes.max_live || this.#next >= Number.MAX_SAFE_INTEGER) {
      this.lose('lifetime_capacity'); return;
    }
    const token = this.#next++;
    const started = monotonicNs();
    const item: ITracked = { id: token, type, name, resource: type.resource, released: false, metrics: this.#collecting(),
      started, epoch: epochNs(started), parent };
    this.#registry.register(value, token, item);
    this.#instances.set(token, item); this.#identities.set(value, token); this.#admissions++;
    if (item.resource) this.#openResources++;
    if (item.metrics) {
      this.#admitted.add(1, { type: name, boundary: 'observed_gc' });
      if (item.resource) this.#admitted.add(1, { type: name, boundary: 'resource' });
    }
    this.gauges();
  }

  released(value: object, name: string, site: number, owner: object | undefined): void {
    if (this.#closed || owner === undefined) return;
    const token = this.#identities.get(value);
    const item = token === undefined ? undefined : this.#instances.get(token);
    if (item?.name !== name || !item.resource || item.released
        || (item.type !== this.#constructors.get(owner) && item.type.keys !== owner) || !item.type.sites.has(site)) return;
    item.released = true; this.#resourceCompleted++; this.#openResources--;
    this.#complete(item, 'resource');
    this.gauges();
  }

  failedRelease(value: object, name: string, site: number, owner: object | undefined): void {
    if (this.#closed || owner === undefined) return;
    const token = this.#identities.get(value);
    const item = token === undefined ? undefined : this.#instances.get(token);
    if (item?.name === name && (item.type === this.#constructors.get(owner) || item.type.keys === owner) && item.type.sites.has(site)) this.#failedReleases++;
  }

  lose(reason: string): void {
    this.#losses[reason] = (this.#losses[reason] ?? 0) + 1;
    this.#lost.add(1, { type: 'all', boundary: reason });
  }

  #collected(token: number): void {
    if (this.#closed) return;
    try {
      const item = this.#instances.get(token);
      if (!item) { this.lose('stale_completion'); return; }
      this.#instances.delete(token); this.#registry.unregister(item); this.#gcCompleted++;
      if (item.resource && !item.released) this.#openResources--;
      this.#complete(item, 'observed_gc');
      if (item.resource && !item.released && item.metrics) {
        this.#incomplete.add(1, { type: item.name, boundary: 'resource' });
      }
      this.gauges();
    } catch { this.lose('invalid'); }
  }

  #complete(item: ITracked, boundary: string): void {
    const elapsed = monotonicNs() - item.started;
    const labels = { type: item.name, boundary };
    if (item.metrics) { this.#completed.add(1, labels); this.#duration.record(Number(elapsed) / 1e9, labels); }
    if (!this.#plan.traces.enabled) return;
    const links = item.parent && isSpanContextValid(item.parent) ? [{ context: item.parent }] : [];
    this.#tracer.startSpan(item.name + ' lifetime', { startTime: sdkTime(item.epoch),
      attributes: { ...labels, 'otelc.lifetime.instance': String(item.id) }, links }, ROOT_CONTEXT)
      .end(sdkTime(item.epoch + elapsed));
  }

  status() {
    return { admitted: this.#admissions, gc_completed: this.#gcCompleted, resource_completed: this.#resourceCompleted,
      current_live: this.#closed ? this.#censored : this.#instances.size, open_resources: this.#openResources,
      tracked_instances: this.#instances.size, tracked_constructors: this.#constructorEntries.size,
      failed_initialisations: this.#failedConstructions, failed_releases: this.#failedReleases,
      censored: this.#censored, lost: Object.values(this.#losses).reduce((total, value) => total + value, 0),
      losses: { ...this.#losses } };
  }

  close(): void {
    if (this.#closed) return;
    this.#closed = true; this.#censored = this.#instances.size;
    this.#censoredResources = this.#openResources;
    for (const item of this.#instances.values()) this.#registry.unregister(item);
    for (const entry of this.#constructorEntries) this.#classes.unregister(entry);
    this.#instances.clear(); this.#constructorEntries.clear();
  }
}
