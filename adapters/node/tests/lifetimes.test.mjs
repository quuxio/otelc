import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Module } from 'node:module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { JsonTraceSerializer } from '@opentelemetry/otlp-transformer';
import { Runtime, RUNTIME } from '../runtime.mjs';
import { transform } from '../transform.mjs';
import { plan, receiver, child } from './helpers.mjs';

function collectionPlan(endpoint) {
  return { ...plan(endpoint),
    traces: { enabled: true, root_sample_ratio: 1, max_active_traces: 8, max_spans_per_trace: 32 },
    trace_export: { endpoint: endpoint.replace('/v1/metrics', '/v1/traces'), protocol: 'http/protobuf', timeout_ms: 300 },
    export: { ...plan(endpoint).export, max_queued_batches: 8 },
    lifetimes: { enabled: true, boundary: 'collection', resources: [] },
    lifetime_matchers: { include: ['(?-u).*'], exclude: [] },
    runtime: { ...plan(endpoint).runtime, max_live_lifetimes: 8 } };
}
function compile(source, runtime) {
  const module = new Module('/tmp/life.cjs');
  module._compile(transform(source, '/tmp/life.cjs', 'life.cjs', runtime.plan, runtime).code, '/tmp/life.cjs');
  return module.exports;
}
test('externally selected classes admit only successful construction without changing receivers or fields', async () => {
  const server = await receiver();
  const p = collectionPlan(server.endpoint); p.function_matchers.include = [];
  const runtime = new Runtime(p); globalThis[RUNTIME] = runtime;
  try {
    const Example = compile(`class Example { #value; constructor(value){if(value<0)throw value;this.#value=value;this.target=new.target;}
      value(){return this.#value;} } module.exports=Example;`, runtime);
    const instance = new Example(7);
    assert.equal(instance.value(), 7); assert.equal(instance.target, Example);
    assert.deepEqual(Reflect.ownKeys(instance), ['target']);
    assert.deepEqual(Reflect.ownKeys(Example.prototype), ['constructor', 'value']);
    assert.throws(() => new Example(-1), value => value === -1);
    assert.equal(runtime.report().lifetimes.admitted, 1);
    assert.equal(runtime.report().lifetimes.failed_initialisations, 1);
    assert.equal(runtime.report().function_calls, 0);
    await runtime.close();
    assert.equal(runtime.report().lifetimes.censored, 1);
    assert.equal(runtime.report().lifetimes.completed, 0);
  } finally { await runtime.close(); delete globalThis[RUNTIME]; await server.close(); }
});

async function usingLifetimes(action, configure = () => {}) {
  const server = await receiver(), p = collectionPlan(server.endpoint); configure(p);
  const runtime = new Runtime(p), exported = [];
  const original = runtime.traces.exporter.export.bind(runtime.traces.exporter);
  runtime.traces.exporter.export = (spans, deadline) => {
    const decoded = JSON.parse(Buffer.from(JsonTraceSerializer.serializeRequest(spans)).toString());
    exported.push(...decoded.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans)));
    return original(spans, deadline);
  };
  globalThis[RUNTIME] = runtime;
  try { await action(runtime, exported, server); }
  finally { await runtime.close(); delete globalThis[RUNTIME]; await server.close(); }
}
test('collection produces a standalone linked span after its creator has completed', async () => usingLifetimes(async (runtime, spans) => {
  const create = compile('class Item{} module.exports=function create(){return new Item;};', runtime);
  const value = create(), [token] = runtime.lifetimes.instances.keys();
  assert.equal(runtime.traces.roots.size, 0, 'a live object kept its creator tree open');
  runtime.lifetimes.collected(token);
  await runtime.close();
  const creator = spans.find(span => span.name === 'life.create');
  const lifetime = spans.find(span => span.name === 'life.Item lifetime');
  assert.ok(value); assert.ok(creator); assert.ok(lifetime);
  assert.ok(!lifetime.parentSpanId); assert.notEqual(lifetime.traceId, creator.traceId);
  assert.equal(lifetime.links.length, 1); assert.equal(lifetime.links[0].traceId, creator.traceId); assert.equal(lifetime.links[0].spanId, creator.spanId);
  assert.equal(runtime.report().lifetimes.completed, 1); assert.equal(runtime.traces.retained, 0);
  const attributes = Object.fromEntries(lifetime.attributes.map(row => [row.key, row.value.stringValue]));
  assert.equal(attributes['code.type.name'], 'life.Item'); assert.equal(attributes['otelc.lifetime.boundary'], 'collection');
  assert.match(attributes['otelc.lifetime.instance'], /^\d+$/);
}, p => { p.function_matchers.include = ['(?-u)^life.create$']; }));

test('type selection, class forms, admission bounds and sticky metrics are independent of function selection', async () => usingLifetimes(async runtime => {
  const { Item, Hidden } = compile('class Item { constructor(done){if(done)return; function local(){return 7;} this.value=local();} } class Hidden{} module.exports={Item,Hidden};', runtime);
  new Hidden; const first = new Item(false), second = new Item(true);
  assert.equal(first.value, 7); assert.deepEqual(Reflect.ownKeys(second), []);
  assert.equal(runtime.lifetimes.admitted, 1); assert.equal(runtime.lifetimes.losses.lifetime_capacity, 1);
  const [token] = runtime.lifetimes.instances.keys();
  runtime.enabled = false; runtime.lifetimes.collected(token);
  assert.deepEqual(runtime.lifetimes.types.get('life.Item'), { admitted: 1, completed: 1, live: 0 });
  new Item; const [next] = runtime.lifetimes.instances.keys(); assert.notEqual(next, token);
  runtime.enabled = true; runtime.lifetimes.collected(next);
  assert.equal(runtime.lifetimes.completed, 2); assert.equal(runtime.report().function_calls, 0);
  assert.deepEqual(runtime.lifetimes.types.get('life.Item'), { admitted: 1, completed: 1, live: 0 });
  runtime.lifetimes.collected(next); assert.equal(runtime.lifetimes.losses.stale_completion, 1);
  const snapshot = await runtime.reader.collect();
  const histogram = snapshot.resourceMetrics.scopeMetrics.flatMap(scope => scope.metrics).find(metric => metric.descriptor.name === 'otelc.lifetime.duration');
  assert.equal(histogram.descriptor.unit, 's'); assert.equal(histogram.dataPoints[0].value.count, 1);
  assert.deepEqual(histogram.dataPoints[0].value.buckets.boundaries, runtime.plan.metrics.histogram_boundaries_seconds);
  assert.deepEqual(Object.keys(histogram.dataPoints[0].attributes).sort(), ['code.type.name', 'otelc.lifetime.boundary']);
}, p => { p.function_matchers.include = []; p.lifetime_matchers.exclude = ['(?-u).*Hidden']; p.runtime.max_live_lifetimes = 1; }));

test('unsupported constructor semantics refuse transformation or observation without changing application behaviour', async () => usingLifetimes(async runtime => {
  for (const source of ['class A extends Object{}', 'class A{constructor(){return {};}}', 'class A{constructor(){eval("1");}}']) {
    assert.throws(() => compile(source, runtime), /not qualified|unsupported/);
  }
  const Base = compile('class Base{constructor(failure){if(failure)throw failure;}} module.exports=Base;', runtime);
  class Derived extends Base {}
  const derived = new Derived; assert.ok(derived instanceof Derived);
  const proxy = new Proxy(Base, {}); assert.ok(new proxy instanceof Base);
  assert.equal(runtime.lifetimes.admitted, 0); assert.equal(runtime.lifetimes.losses.unsupported_constructor, 2);
  const failure = { original: true };
  assert.throws(() => new Derived(failure), value => value === failure);
  assert.equal(runtime.lifetimes.failed, 0); assert.equal(runtime.lifetimes.losses.unsupported_constructor, 3);
  const value = new Base; const token = runtime.lifetimes.identities.get(value);
  runtime.lifetimes.admit(value, Base, 'life.Base', null); assert.equal(runtime.lifetimes.losses.duplicate_initialisation, 1);
  runtime.lifetimes.collected(token);
}, p => { p.function_matchers.include = []; }));

test('anonymous expressions, static construction and hostile payload hooks need no added layout members', async () => usingLifetimes(async runtime => {
  const api = compile(`const Alias=class {static instance=new this; constructor(){Object.defineProperty(this,'constructor',{get(){throw 9;}});}
    [Symbol.toPrimitive](){throw 8;}}; module.exports=Alias;`, runtime);
  assert.equal(api.name, 'Alias'); const value = new api;
  assert.deepEqual(Reflect.ownKeys(value), ['constructor']);
  assert.equal(runtime.lifetimes.admitted, 2);
  for (const token of [...runtime.lifetimes.instances.keys()]) runtime.lifetimes.collected(token);
  assert.deepEqual(runtime.lifetimes.report().losses, {});
}, p => { p.function_matchers.include = []; }));

test('sampling and queue limits never change lifetime counts, and shutdown censors live instances', async () => {
  for (const ratio of [0, 1]) await usingLifetimes(async (runtime, spans) => {
    const Item = compile('module.exports=class Item{};', runtime);
    const values = [new Item, new Item, new Item];
    const tokens = [...runtime.lifetimes.instances.keys()];
    runtime.lifetimes.collected(tokens[0]); runtime.lifetimes.collected(tokens[1]);
    await runtime.close();
    assert.equal(values.length, 3); const report = runtime.report().lifetimes;
    assert.equal(report.admitted, 3); assert.equal(report.completed, 2); assert.equal(report.censored, 1);
    assert.equal(report.current_live, 1); assert.equal(report.tracked_instances, 0); assert.equal(report.losses.incomplete, 1);
    assert.equal(spans.length, ratio); assert.equal(runtime.traces.retained, 0);
    assert.equal(runtime.report().traces.losses.queue_capacity ?? 0, ratio);
    runtime.lifetimes.collected(tokens[2]); assert.equal(runtime.lifetimes.completed, 2);
  }, p => { p.function_matchers.include = []; p.traces.root_sample_ratio = ratio; p.export.max_queued_batches = 1; });
});

test('lifetime root sampling is independent of a rejected creator observation', async () => usingLifetimes(async (runtime, spans) => {
  const Item = compile('module.exports=class Item{};', runtime);
  runtime.exit(runtime.enter('occupied'), false);
  const rejected = runtime.enter('rejected');
  assert.equal(rejected, 0);
  const previous = runtime.attach(rejected);
  const value = new Item;
  runtime.detach(previous);
  const [token] = runtime.lifetimes.instances.keys();
  runtime.lifetimes.collected(token); await runtime.close();
  assert.ok(value); assert.equal(runtime.lifetimes.completed, 1);
  const lifetime = spans.find(span => span.name === 'life.Item lifetime');
  assert.ok(lifetime, 'a separate lifetime root inherited function-observation rejection');
  assert.ok(!lifetime.parentSpanId); assert.equal(lifetime.links.length, 0);
  assert.equal(runtime.report().traces.losses.function_capacity, 1);
  assert.deepEqual(runtime.report().lifetimes.losses, {});
}, p => { p.function_matchers.include = []; p.runtime.max_functions = 1; }));

test('documented unedited and annotated examples preserve bytes and expose type selection', async () => {
  const server = await receiver(), directory = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-js-example-'));
  const root = fileURLToPath(new URL('../../../', import.meta.url));
  try {
    for (const name of ['javascript_collection_app.cjs', 'javascript_collection_annotated.cjs']) {
      const source = path.join(root, 'examples/apps', name), original = fs.readFileSync(source);
      const baseline = await child(process.execPath, [source]);
      const report = path.join(directory, name + '.json');
      const measured = await child(path.join(root, 'target/debug/quux-otelc'), ['--config', 'examples/javascript-collection.toml', 'node', source],
        { OTEL_EXPORTER_OTLP_ENDPOINT: server.endpoint.replace('/v1/metrics', ''), OTEL_EXPORTER_OTLP_METRICS_ENDPOINT: server.endpoint,
          OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: server.endpoint.replace('/v1/metrics', '/v1/traces'),
          OTEL_EXPORTER_OTLP_HEADERS: '', OTEL_EXPORTER_OTLP_METRICS_HEADERS: '', OTEL_EXPORTER_OTLP_TRACES_HEADERS: '', OTELC_REPORT_PATH: report });
      assert.deepEqual(measured, baseline); assert.equal(baseline.stdout, '48\n'); assert.equal(baseline.status, 0);
      assert.deepEqual(fs.readFileSync(source), original);
      const result = JSON.parse(fs.readFileSync(report)); assert.equal(result.lifetimes.admitted, 2);
      assert.equal(result.lifetimes.completed, 0); assert.equal(result.lifetimes.censored, 2);
      const inspected = await child(path.join(root, 'target/debug/quux-otelc'), ['--config', 'examples/javascript-collection.toml', '--language', 'javascript', 'inspect', source, '--json']);
      assert.equal(inspected.status, 0, inspected.stderr);
      const inventory = JSON.parse(inspected.stdout); assert.equal(inventory.types.filter(type => type.selected).length, 1);
    }
  } finally { await server.close(); fs.rmSync(directory, { recursive: true, force: true }); }
});

test('invalid resolved settings fail before SDK setup', () => {
  for (const change of [p => { p.language = 'typescript'; }, p => { p.traces.enabled = false; },
    p => { p.lifetimes.boundary = 'object'; }, p => { p.lifetimes.enabled = 'true'; }, p => { p.lifetimes.resources = [{}]; },
    p => { p.runtime.max_live_lifetimes = 0; }, p => { p.runtime.max_live_lifetimes = '8'; },
    p => { p.runtime.max_live_lifetimes = 65537; }, p => { p.runtime.max_functions = 0; }, p => { p.runtime.max_functions = 65537; }, p => { p.lifetime_matchers = null; }]) {
    const p = collectionPlan('http://127.0.0.1:1/v1/metrics'); change(p);
    assert.throws(() => new Runtime(p), /JavaScript lifetimes require/);
  }
});

test('telemetry failures cannot replace a successful constructor or prevent completion bookkeeping', async () => usingLifetimes(async runtime => {
  const Item = compile('module.exports=class Item{};', runtime);
  const start = runtime.traces.beginLifetime.bind(runtime.traces);
  runtime.traces.beginLifetime = () => { throw Error('private'); };
  assert.ok(new Item); assert.equal(runtime.lifetimes.losses.invalid, 1);
  runtime.traces.beginLifetime = start;
  const value = new Item; const [token] = runtime.lifetimes.instances.keys();
  runtime.lifetimes.duration.record = () => { throw Error('private'); };
  runtime.lifetimes.collected(token);
  assert.ok(value); assert.equal(runtime.lifetimes.completed, 1); assert.equal(runtime.lifetimes.losses.invalid, 2);
  await runtime.close(); assert.equal(runtime.traces.retained, 0);
}, p => { p.function_matchers.include = []; }));

test('constructor metadata and type cardinality are bounded independently of live instances', async () => usingLifetimes(async runtime => {
  const factory = compile('module.exports=function factory(){return class Same{};};', runtime);
  const first = factory(), second = factory();
  assert.equal(first.name, 'Same'); assert.equal(second.name, 'Same');
  assert.ok(new first); assert.ok(new second);
  assert.equal(runtime.lifetimes.constructorEntries.size, 1); assert.equal(runtime.lifetimes.admitted, 1);
  assert.equal(runtime.lifetimes.losses.constructor_capacity, 1);
  runtime.lifetimes.type(first, 'life.factory.Same'); assert.equal(runtime.lifetimes.constructorEntries.size, 1);
}, p => { p.function_matchers.include = []; p.runtime.max_functions = 1; }));

test('field failures, frozen instances and SDK completion failure preserve declared boundaries', async () => usingLifetimes(async runtime => {
  const api = compile(`const failure={original:true};class Broken{field=(()=>{throw failure;})();}
    class Frozen{constructor(){Object.freeze(this);}}module.exports={Broken,Frozen,failure};`, runtime);
  assert.throws(() => new api.Broken, value => value === api.failure);
  assert.equal(runtime.lifetimes.admitted, 0); assert.equal(runtime.lifetimes.failed, 0);
  const value = new api.Frozen; assert.ok(Object.isFrozen(value)); assert.deepEqual(Reflect.ownKeys(value), []);
  const [token, item] = [...runtime.lifetimes.instances][0];
  item.span.span.end = () => { throw Error('private SDK failure'); };
  runtime.lifetimes.collected(token);
  assert.equal(runtime.lifetimes.completed, 1); assert.equal(runtime.traces.retained, 0);
  assert.equal(runtime.traces.losses.invalid, 1);
}, p => { p.function_matchers.include = []; }));

async function gcFixture(application, typeName, verify, extension = 'cjs') {
  const server = await receiver(), directory = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-js-life-'));
  const source = path.join(directory, 'gc.' + extension), policy = path.join(directory, 'plan.json'), report = path.join(directory, 'report.json'), capture = path.join(directory, 'spans.jsonl');
  fs.writeFileSync(source, application);
  const original = createHash('sha256').update(fs.readFileSync(source)).digest('hex');
  const p = collectionPlan(server.endpoint); p.function_matchers.include = ['(?-u)^gc.make$']; fs.writeFileSync(policy, JSON.stringify(p));
  const register = fileURLToPath(new URL('../register.mjs', import.meta.url));
  const serializer = fileURLToPath(import.meta.resolve('@opentelemetry/otlp-transformer'));
  const wrapper = `import fs from 'node:fs';import {pathToFileURL} from 'node:url';import {install} from ${JSON.stringify(register)};import {JsonTraceSerializer} from ${JSON.stringify(serializer)};
const app=await install(JSON.parse(fs.readFileSync(process.argv[1])),${JSON.stringify(directory)});const original=app.runtime.traces.exporter.export.bind(app.runtime.traces.exporter);
app.runtime.traces.exporter.export=(spans,deadline)=>{fs.appendFileSync(${JSON.stringify(capture)},Buffer.from(JsonTraceSerializer.serializeRequest(spans)).toString()+'\\n');return original(spans,deadline);};
try{const module=await import(pathToFileURL(process.argv[2]));await module.default;fs.writeFileSync(${JSON.stringify(path.join(directory, 'preclose.json'))},JSON.stringify(app.runtime.report()));}finally{await app.close();}`;
  try {
    const baseline = await child(process.execPath, ['--expose-gc', source]);
    const instrumented = await child(process.execPath, ['--expose-gc', '--input-type=module', '-e', wrapper, policy, source], { OTELC_REPORT_PATH: report });
    assert.equal(baseline.status, 0, baseline.stderr); assert.deepEqual(instrumented, baseline);
    assert.equal(createHash('sha256').update(fs.readFileSync(source)).digest('hex'), original);
    const result = JSON.parse(fs.readFileSync(report)); assert.equal(result.lifetimes.admitted, 1); assert.equal(result.lifetimes.completed, 1);
    assert.equal(result.lifetimes.current_live, 0); assert.deepEqual(result.lifetimes.losses, {}); assert.equal(result.export_loss, 0);
    const spans = fs.readFileSync(capture, 'utf8').trim().split('\n').flatMap(line => JSON.parse(line).resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans)));
    const life = spans.find(span => span.name === typeName + ' lifetime'), creator = spans.find(span => span.name === 'gc.make');
    assert.ok(life); assert.ok(creator); assert.equal(life.links[0].spanId, creator.spanId);
    assert.ok(server.requests.some(request => request.url === '/v1/traces' && request.body.length));
    verify?.(JSON.parse(fs.readFileSync(path.join(directory, 'preclose.json'))));
  } finally { await server.close(); fs.rmSync(directory, { recursive: true, force: true }); }
}
test('real GC collects cycles without observer retention and leaves original source and channels unchanged', async () => gcFixture(`class Item{#value=7;constructor(){this.self=this;}read(){return this.#value;}}
let collected=0;const registry=new FinalizationRegistry(()=>{collected++;});
function make(){const value=new Item;registry.register(value,1);console.log(value.read(),Object.keys(value).join(','),value.constructor.name);}
module.exports=(async()=>{make();for(let n=0;n<100;n++){await new Promise(setImmediate);global.gc();if(collected){for(let m=0;m<4;m++)await new Promise(setImmediate);break;}}if(collected!==1)throw Error('collection not observed');console.log('collected',collected);})();`, 'gc.Item'));

test('real GC releases dynamically created constructor metadata while the observer remains active', async () => gcFixture(`let collected=0;const registry=new FinalizationRegistry(()=>{collected++;});
function make(){class Item{constructor(){this.self=this;}}const value=new Item;registry.register(Item,1);registry.register(value,2);console.log(Item.name);}
module.exports=(async()=>{make();for(let n=0;n<100;n++){await new Promise(setImmediate);global.gc();if(collected===2){for(let m=0;m<4;m++)await new Promise(setImmediate);break;}}if(collected!==2)throw Error('collection not observed');console.log('collected',collected);})();`, 'gc.make.Item', report => {
  assert.equal(report.lifetimes.tracked_constructors, 0); assert.equal(report.lifetimes.tracked_instances, 0);
}));

test('ES modules preserve source and channels while collecting a selected class', async () => gcFixture(`class Item{constructor(){this.self=this;}}
let collected=0;const registry=new FinalizationRegistry(()=>{collected++;});
function make(){const value=new Item;registry.register(value,1);console.log('created');}
export default(async()=>{make();for(let n=0;n<100;n++){await new Promise(setImmediate);global.gc();if(collected){for(let m=0;m<4;m++)await new Promise(setImmediate);break;}}if(collected!==1)throw Error('collection not observed');console.log('collected',collected);})();`, 'gc.Item', undefined, 'mjs'));
