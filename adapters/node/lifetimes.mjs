import { Selection } from './policy.mjs';

let nextToken = 1;
const Registry = globalThis.FinalizationRegistry;
export function validateLifetimes(plan) {
  if (!plan.lifetimes?.enabled) return;
  try {
    if (!['javascript', 'typescript'].includes(plan.language) || plan.lifetimes.enabled !== true || plan.lifetimes.boundary !== 'collection' || !plan.traces?.enabled
        || !Array.isArray(plan.lifetimes.resources) || plan.lifetimes.resources.length
        || !Number.isSafeInteger(plan.runtime.max_live_lifetimes) || plan.runtime.max_live_lifetimes < 1 || plan.runtime.max_live_lifetimes > 65536
        || !Number.isSafeInteger(plan.runtime.max_functions) || plan.runtime.max_functions < 1 || plan.runtime.max_functions > 65536 || typeof Registry !== 'function') throw new Error();
    new Selection(plan.lifetime_matchers);
  } catch { throw new Error('JavaScript/TypeScript lifetimes require tracing, collection and bounded resolved settings'); }
}

export class Lifetimes {
  constructor(runtime, meter) {
    this.runtime = runtime;
    this.types = new Map(); this.constructors = new WeakMap(); this.constructorEntries = new Set(); this.identities = new WeakMap(); this.instances = new Map();
    this.admitted = 0; this.completed = 0; this.censored = 0; this.failed = 0; this.closed = false; this.losses = {};
    this.registry = new Registry(token => this.collected(token));
    this.register = this.registry.register.bind(this.registry);
    this.unregister = this.registry.unregister.bind(this.registry);
    this.classRegistry = new Registry(entry => this.constructorEntries.delete(entry));
    this.registerClass = this.classRegistry.register.bind(this.classRegistry);
    this.unregisterClass = this.classRegistry.unregister.bind(this.classRegistry);
    this.duration = meter.createHistogram('otelc.lifetime.duration', { unit: 's' });
    for (const kind of ['admitted', 'completed', 'live']) {
      const instrument = kind === 'live' ? meter.createObservableGauge('otelc.lifetime.live', { unit: '{instance}' }) : meter.createObservableCounter('otelc.lifetime.' + kind, { unit: '{instance}' });
      instrument.addCallback(observer => {
        for (const [name, row] of this.types) observer.observe(row[kind], { 'code.type.name': name, 'otelc.lifetime.boundary': 'collection' });
      });
    }
    meter.createObservableCounter('otelc.lifetime.dropped_observations', { unit: '{observation}' }).addCallback(observer => {
      for (const [reason, count] of Object.entries(this.losses)) observer.observe(count, { reason });
    });
    meter.createObservableCounter('otelc.lifetime.failed_initialisations', { unit: '{instance}' }).addCallback(observer => observer.observe(this.failed));
  }
  lose(reason) { this.losses[reason] = (this.losses[reason] ?? 0) + 1; }
  failedConstruction(constructor, name) {
    if (this.closed) return;
    if (this.constructors.get(constructor)?.name === name) this.failed++;
    else this.lose('unsupported_constructor');
  }
  type(constructor, name) {
    if (this.closed) return;
    if (this.constructors.has(constructor)) return;
    if (this.constructorEntries.size >= this.runtime.plan.runtime.max_functions) { this.lose('constructor_capacity'); return; }
    if (!this.types.has(name)) {
      if (this.types.size >= this.runtime.plan.runtime.max_functions || Buffer.byteLength(name) > 1024) { this.lose('type_capacity'); return; }
      this.types.set(name, { admitted: 0, completed: 0, live: 0 });
    }
    const entry = { name };
    this.registerClass(constructor, entry, entry);
    this.constructorEntries.add(entry); this.constructors.set(constructor, entry);
  }
  admit(value, constructor, name, creation) {
    if (this.closed) return;
    if (this.constructors.get(constructor)?.name !== name) { this.lose('unsupported_constructor'); return; }
    if (this.identities.has(value)) { this.lose('duplicate_initialisation'); return; }
    if (this.instances.size >= this.runtime.plan.runtime.max_live_lifetimes) { this.lose('lifetime_capacity'); return; }
    if (nextToken >= Number.MAX_SAFE_INTEGER) { this.lose('invalid'); return; }
    const token = nextToken++, started = process.hrtime.bigint();
    const span = this.runtime.traces.beginLifetime(name, token, creation, started);
    const item = { name, started, span, metrics: this.runtime.enabled };
    try { this.register(value, token, item); }
    catch (error) { this.runtime.traces.discardLifetime(span); throw error; }
    this.instances.set(token, item); this.identities.set(value, token); this.admitted++;
    if (item.metrics) { const row = this.types.get(name); row.admitted++; row.live++; }
  }
  collected(token) {
    if (this.closed) return;
    try {
      const item = this.instances.get(token);
      if (!item) { this.lose('stale_completion'); return; }
      this.instances.delete(token); this.unregister(item); this.completed++;
      const elapsed = process.hrtime.bigint() - item.started;
      if (item.metrics) {
        const row = this.types.get(item.name); row.completed++; row.live--;
        try { this.duration.record(Number(elapsed > 0n ? elapsed : 0n) / 1e9, { 'code.type.name': item.name, 'otelc.lifetime.boundary': 'collection' }); }
        catch { this.lose('invalid'); }
      }
      this.runtime.traces.finishLifetime(item.span, elapsed);
    } catch { this.lose('invalid'); }
  }
  close() {
    this.closed = true;
    this.censored += this.instances.size;
    if (this.instances.size) this.losses.incomplete = (this.losses.incomplete ?? 0) + this.instances.size;
    for (const item of this.instances.values()) { this.unregister(item); this.runtime.traces.discardLifetime(item.span); }
    for (const entry of this.constructorEntries) this.unregisterClass(entry);
    this.constructorEntries.clear();
    this.instances.clear(); this.identities = new WeakMap(); this.constructors = new WeakMap();
  }
  report() {
    return { admitted: this.admitted, completed: this.completed, current_live: this.censored + this.instances.size, censored: this.censored,
      tracked_instances: this.instances.size, tracked_constructors: this.constructorEntries.size, failed_initialisations: this.failed, losses: { ...this.losses } };
  }
}
