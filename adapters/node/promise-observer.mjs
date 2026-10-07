import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { promiseHooks } from 'node:v8';

const require = createRequire(import.meta.url);
export function nativeObserver() {
  try { return require(process.env.OTELC_NODE_OBSERVER ?? fileURLToPath(new URL('../../target/debug/otelc_node_observer.node', import.meta.url))); }
  catch (cause) { throw new Error('Build the Promise observer for this Node version with make node-build (or set OTELC_NODE_OBSERVER)', { cause }); }
}
const physical = filename => filename.startsWith('file:') ? fileURLToPath(filename) : filename;
const position = frame => [frame[2], frame[3]];
const compare = (a, b) => a[0] - b[0] || a[1] - b[1];
const caller = (frames, index, origin) => JSON.stringify([frames[index][1], origin, frames.slice(index + 1).map(frame => frame.slice(1))]);

// No then/await/Promise reactions, no result inspection, no rejection handler.
// V8 fires settled just before changing State; finalise on the next snapshot or
// probe rather than inserting another application-visible microtask.
export class PromiseObserver {
  constructor(runtime) {
    this.runtime = runtime;
    this.native = nativeObserver();
    this.sites = new Map();
    this.origins = new Set();
    this.roots = new Map();
    this.records = new WeakMap();
    this.active = new Map();
    this.settled = new Set();
    const safe = action => promise => {
      try { action(promise); } catch { this.runtime.losses.invalid++; }
    };
    this.stop = promiseHooks.createHook({ init: safe(promise => this.init(promise)), settled: safe(promise => this.settle(promise)) });
  }
  register(sites) {
    for (const site of sites) {
      if (this.sites.size >= this.runtime.plan.runtime.max_functions) { this.runtime.losses.function_capacity++; continue; }
      this.sites.set(site.id, site);
      this.origins.add(JSON.stringify([site.filename, ...site.origin]));
    }
  }
  init(promise) {
    if (!this.runtime.observing || this.runtime.closed) return;
    const frames = this.native.frames();
    const index = frames.findIndex(frame => this.origins.has(JSON.stringify([physical(frame[0]), ...position(frame)])));
    if (index < 0) return;
    this.flush();
    if (frames.length === 128 || this.roots.size + this.active.size >= this.runtime.plan.runtime.max_active_calls) return;
    const key = caller(frames, index, position(frames[index]));
    const record = { promise: new WeakRef(promise), key, token: 0, end: null, tracked: true };
    this.roots.set(key, record);
    this.records.set(promise, record);
  }
  enter(name, id) {
    if (!this.runtime.observing || this.runtime.closed) return 0;
    this.flush();
    const site = this.sites.get(id);
    if (!site) { this.runtime.losses.async_origin++; return this.runtime.rejectTrace('async_origin'); }
    const frames = this.native.frames();
    const index = frames.findIndex(frame => physical(frame[0]) === site.filename && compare(position(frame), site.start) >= 0 && compare(position(frame), site.end) <= 0);
    const key = index < 0 ? null : caller(frames, index, site.origin);
    const record = frames.length === 128 ? null : this.roots.get(key);
    if (!record || !record.promise.deref()) {
      if (this.roots.size + this.active.size >= this.runtime.plan.runtime.max_active_calls) this.runtime.losses.active_call_capacity++;
      else this.runtime.losses.async_origin++;
      return this.runtime.rejectTrace('async_origin');
    }
    this.roots.delete(key);
    record.token = this.runtime.enter(name);
    if (record.token) this.active.set(record.token, record);
    else record.tracked = false;
    return record.token;
  }
  settle(promise) {
    const record = this.records.get(promise);
    if (!record?.tracked) return;
    record.end = process.hrtime.bigint();
    // Keep the settled Promise alive until State can be read after this hook.
    // A WeakRef alone can disappear between settlement and the next snapshot,
    // falsely reporting a completed call as incomplete. This set is bounded by
    // the shared active-call limit and never reads Promise::Result().
    record.settledPromise = promise;
    this.settled.add(record);
  }
  flush() {
    for (const record of this.settled) {
      const promise = record.settledPromise ?? record.promise.deref();
      const state = promise ? this.native.state(promise) : 0;
      if (promise && !state) continue;
      if (record.token && state) this.runtime.exit(record.token, state === 2, record.end);
      // If an application discards a pending Promise, its unfinished call stays
      // in the bounded runtime map and is reported as incomplete at shutdown.
      this.settled.delete(record);
      this.active.delete(record.token);
      record.tracked = false;
      record.settledPromise = null;
      if (this.roots.get(record.key) === record) this.roots.delete(record.key);
    }
    for (const [key, record] of this.roots) if (!record.promise.deref()) this.roots.delete(key);
  }
  close() {
    this.flush();
    this.stop();
    this.roots.clear(); this.active.clear(); this.settled.clear(); this.sites.clear(); this.origins.clear();
  }
}
