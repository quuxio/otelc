import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { Module } from 'node:module';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { Runtime, RUNTIME } from '../runtime.mjs';
import { transpile } from '../typescript.mjs';
import { transform } from '../transform.mjs';
import { install } from '../register.mjs';
import { plan, receiver, child } from './helpers.mjs';

function typedPlan(endpoint, backend) {
  return { ...plan(endpoint), language: 'typescript', backend,
    export: { ...plan(endpoint).export, max_queued_batches: 8 },
    function_matchers: { include: [], exclude: [] }, lifetime_matchers: { include: ['(?-u).*'], exclude: [] },
    lifetimes: { enabled: true, boundary: 'collection', resources: [] },
    traces: { enabled: true, root_sample_ratio: 1, max_active_traces: 8, max_spans_per_trace: 32 },
    trace_export: { endpoint: endpoint.replace('/v1/metrics', '/v1/traces'), protocol: 'http/protobuf', timeout_ms: 300 },
    runtime: { ...plan(endpoint).runtime, max_live_lifetimes: 16 } };
}
function compile(source, filename, p, root, runtime = null) {
  const emitted = transpile(source, filename, path.basename(filename), p, root);
  const code = runtime ? transform(emitted.code, filename, path.basename(filename), p, runtime, emitted).code : emitted.code;
  const module = new Module(filename); module._compile(code, filename); return module.exports;
}
for (const backend of ['source', 'native']) for (const target of ['ES2018', 'ES2022']) {
  test(`${backend} ${target} collection preserves original typed identities and constructor behaviour`, async () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-ts-life-')), server = await receiver();
    const filename = path.join(directory, 'app.cts'), p = typedPlan(server.endpoint, backend);
    fs.writeFileSync(path.join(directory, 'tsconfig.json'), JSON.stringify({ compilerOptions: { target } }));
    const source = `export namespace Outer { export class Item<T> { #value:T;
      constructor(public readonly value:T, fail=false){this.#value=value;if(fail)throw value;}
      read(){return this.#value;} } }
      export const Alias=class { static held = new this; readonly answer=7; };
      export class Empty { readonly answer=9; }`;
    fs.writeFileSync(filename, source);
    const baseline = compile(source, filename, { ...p, lifetimes: { enabled: false } }, directory);
    let runtime;
    try {
      runtime = new Runtime(p); globalThis[RUNTIME] = runtime;
      const api = compile(source, filename, p, directory, runtime);
      const value = new api.Outer.Item(42), expected = new baseline.Outer.Item(42);
      assert.equal(value.read(), expected.read()); assert.deepEqual(Reflect.ownKeys(value), Reflect.ownKeys(expected));
      assert.deepEqual(Reflect.ownKeys(api.Outer.Item.prototype), Reflect.ownKeys(baseline.Outer.Item.prototype));
      const failure = { original: true }; assert.throws(() => new api.Outer.Item(failure, true), thrown => thrown === failure);
      assert.equal(new api.Empty().answer, new baseline.Empty().answer); assert.equal(new api.Alias().answer, 7);
      assert.equal(runtime.report().lifetimes.admitted, 4); assert.equal(runtime.report().lifetimes.failed_initialisations, 1);
      assert.deepEqual([...runtime.lifetimes.types.keys()].sort(), ['app.Alias', 'app.Empty', 'app.Outer.Item']);
      assert.equal(fs.readFileSync(filename, 'utf8'), source); await runtime.close();
      assert.equal(runtime.report().lifetimes.censored, 4); assert.equal(runtime.report().lifetimes.completed, 0);
    } finally { await runtime?.close(); delete globalThis[RUNTIME]; await server.close(); fs.rmSync(directory, { recursive: true, force: true }); }
  });
}

for (const backend of ['source', 'native']) {
  test(`${backend} type annotations and exclusions are applied before compiler lowering`, async () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-ts-selection-')), server = await receiver();
    const p = typedPlan(server.endpoint, backend); p.lifetime_matchers = { include: [], exclude: ['(?-u)app.Hidden'] };
    const filename = path.join(directory, 'app.cts'); let runtime;
    try {
      runtime = new Runtime(p); globalThis[RUNTIME] = runtime;
      const source = `export declare class External { constructor(); }
        // otelc.instrument
        export class Selected { constructor(){if(true)return;} }
        // otelc.instrument
        export class Hidden {}
        export class Unselected extends Object {}
        export function make(){return new Selected();}`;
      const api = compile(source, filename, p, directory, runtime);
      new api.Hidden(); new api.Unselected(); const value = api.make(); assert.ok(value instanceof api.Selected);
      assert.equal(runtime.lifetimes.admitted, 1); assert.deepEqual([...runtime.lifetimes.types.keys()], ['app.Selected']);
      assert.throws(() => transpile('class Broken {', filename, 'app.cts', p, directory), /syntax/);
      assert.throws(() => transpile('const forged="@quux.otelc.type bad";', filename, 'app.cts', p, directory), /already prepared/);
      p.annotations.read_existing = false;
      const unselected = compile(source, filename, p, directory, runtime); new unselected.Selected();
      assert.equal(runtime.lifetimes.admitted, 1);
    } finally { await runtime?.close(); delete globalThis[RUNTIME]; await server.close(); fs.rmSync(directory, { recursive: true, force: true }); }
  });
  test(`${backend} unsupported selected typed constructors fail before module execution`, async () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-ts-refusal-')), server = await receiver();
    const filename = path.join(directory, 'app.cts'), p = typedPlan(server.endpoint, backend); let runtime;
    try {
      runtime = new Runtime(p); globalThis[RUNTIME] = runtime;
      for (const source of [
        'class A extends Object {}',
        'function decorate(value:any){return value;} @decorate class A {}',
        'function decorate(...args:any[]){} class A { @decorate method(){} }',
        'class A {constructor(){return {};}}',
        'class A {constructor(){eval("1");}}'
      ]) assert.throws(() => compile('globalThis.__typedLifeExecuted=true;'+source, filename, p, directory, runtime), /not qualified|unsupported/);
      assert.equal(globalThis.__typedLifeExecuted, undefined);
      assert.equal(runtime.lifetimes.admitted, 0);
    } finally { await runtime?.close(); delete globalThis[RUNTIME]; await server.close(); fs.rmSync(directory, { recursive: true, force: true }); }
  });
}

for (const backend of ['source', 'native']) for (const extension of ['ts', 'mts', 'cts']) {
  test(`${backend} .${extension} actual collection preserves source/channels and exports linked lifetime roots`, async () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-ts-gc-')), server = await receiver();
    const source = path.join(directory, 'gc.' + extension), policy = path.join(directory, 'plan.json');
    const report = path.join(directory, 'report.json'), capture = path.join(directory, 'spans.jsonl');
    const p = typedPlan(server.endpoint, backend); p.function_matchers.include = ['(?-u)^gc.make$'];
    fs.writeFileSync(path.join(directory, 'tsconfig.json'), JSON.stringify({ compilerOptions: { target: extension === 'mts' ? 'ES2022' : 'ES2018' } }));
    fs.writeFileSync(path.join(directory, 'package.json'), '{"type":"module"}');
    const original = `let collected=0;const registry=new FinalizationRegistry(()=>{collected++;});
namespace Nested {export class Item<T>{self:unknown;constructor(public value:T){this.self=this;}read(){return this.value;}}}
function make(){const item=new Nested.Item(7);registry.register(item,1);console.log(item.read(),Object.keys(item).join(','),item.constructor.name);}
const finished=(async()=>{make();for(let n=0;n<100;n++){await new Promise(setImmediate);globalThis.gc();if(collected){for(let m=0;m<4;m++)await new Promise(setImmediate);break;}}if(collected!==1)throw Error('collection not observed');console.log('collected',collected);})();export default finished;`;
    fs.writeFileSync(source, original); fs.writeFileSync(policy, JSON.stringify(p));
    const plain = fileURLToPath(new URL('../plain.mjs', import.meta.url)), register = fileURLToPath(new URL('../register.mjs', import.meta.url));
    const serializer = fileURLToPath(import.meta.resolve('@opentelemetry/otlp-transformer'));
    const baselineWrapper = `process.chdir(${JSON.stringify(directory)});await import(${JSON.stringify(plain)});const app=await import(${JSON.stringify(source)});await(app.default?.default??app.default);`;
    const measuredWrapper = `import fs from 'node:fs';import {install} from ${JSON.stringify(register)};import {JsonTraceSerializer} from ${JSON.stringify(serializer)};
process.chdir(${JSON.stringify(directory)});const app=await install(JSON.parse(fs.readFileSync(${JSON.stringify(policy)})),${JSON.stringify(directory)});const original=app.runtime.traces.exporter.export.bind(app.runtime.traces.exporter);
app.runtime.traces.exporter.export=(spans,deadline)=>{fs.appendFileSync(${JSON.stringify(capture)},Buffer.from(JsonTraceSerializer.serializeRequest(spans)).toString()+'\\n');return original(spans,deadline);};
try{const module=await import(${JSON.stringify(source)});await(module.default?.default??module.default);}finally{await app.close();}`;
    try {
      const baseline = await child(process.execPath, ['--expose-gc', '--input-type=module', '-e', baselineWrapper], { OTELC_TYPESCRIPT_BACKEND: backend });
      const measured = await child(process.execPath, ['--expose-gc', '--input-type=module', '-e', measuredWrapper], { OTELC_REPORT_PATH: report });
      assert.equal(baseline.status, 0, baseline.stderr); assert.deepEqual(measured, baseline); assert.equal(measured.stderr, '');
      assert.equal(fs.readFileSync(source, 'utf8'), original);
      const result = JSON.parse(fs.readFileSync(report)); assert.equal(result.lifetimes.admitted, 1);
      assert.equal(result.lifetimes.completed, 1); assert.equal(result.lifetimes.current_live, 0); assert.deepEqual(result.lifetimes.losses, {});
      const spans = fs.readFileSync(capture, 'utf8').trim().split('\n').flatMap(line => JSON.parse(line).resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans)));
      const lifetime = spans.find(span => span.name === 'gc.Nested.Item lifetime'), creator = spans.find(span => span.name === 'gc.make');
      assert.ok(lifetime); assert.ok(creator); assert.ok(!lifetime.parentSpanId); assert.notEqual(lifetime.traceId, creator.traceId);
      assert.equal(lifetime.links.length, 1); assert.equal(lifetime.links[0].spanId, creator.spanId);
      assert.equal(result.export_loss, 0); assert.ok(Object.values(result.traces.losses).every(value => value === 0));
      assert.ok(server.requests.some(request => request.url === '/v1/traces' && request.body.length));
    } finally { await server.close(); fs.rmSync(directory, { recursive: true, force: true }); }
  });
}

for (const backend of ['source', 'native']) test(`${backend} imports outside source selection compile without lifetime probes`, async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-ts-source-filter-')), server = await receiver();
  const p = typedPlan(server.endpoint, backend); p.source_matchers.include = ['(?-u)app\\.mts'];
  const helper = '// otelc.unknown\nexport class Other extends Object {}';
  fs.writeFileSync(path.join(directory, 'helper.mts'), helper);
  const source = "import {Other} from './helper.mts';export class Item{};export const values=[new Other(),new Item()];";
  const filename = path.join(directory, 'app.mts'); fs.writeFileSync(filename, source); let app;
  try {
    app = await install(p, directory); const api = await import(filename); assert.equal(api.values.length, 2);
    assert.equal(app.runtime.lifetimes.admitted, 1); assert.deepEqual([...app.runtime.lifetimes.types.keys()], ['app.Item']);
    assert.equal(fs.readFileSync(filename, 'utf8'), source); assert.equal(fs.readFileSync(path.join(directory, 'helper.mts'), 'utf8'), helper);
  } finally { await app?.close(); await server.close(); fs.rmSync(directory, { recursive: true, force: true }); }
});

test('documented typed examples run unchanged through both compilers and expose original class identities', async () => {
  const server = await receiver(), directory = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-ts-life-examples-'));
  const root = fileURLToPath(new URL('../../../', import.meta.url)), cli = path.join(root, 'target/debug/quux-otelc');
  try {
    for (const backend of ['source', 'native']) {
      const policy = path.join(directory, backend + '.toml');
      fs.writeFileSync(policy, fs.readFileSync(path.join(root, 'examples/typescript-collection.toml'), 'utf8').replace('backend = "source"', 'backend = "' + backend + '"'));
      for (const name of ['typescript_collection_app.mts', 'typescript_collection_annotated.cts']) {
        const filename = path.join(root, 'examples/apps', name), original = fs.readFileSync(filename), report = path.join(directory, name + '.json');
        const baseline = await child(process.execPath, ['--import', './adapters/node/plain.mjs', filename], { OTELC_TYPESCRIPT_BACKEND: backend });
        const measured = await child(cli, ['--config', policy, 'ts', filename], {
          OTEL_EXPORTER_OTLP_ENDPOINT: server.endpoint.replace('/v1/metrics', ''), OTEL_EXPORTER_OTLP_METRICS_ENDPOINT: server.endpoint,
          OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: server.endpoint.replace('/v1/metrics', '/v1/traces'),
          OTEL_EXPORTER_OTLP_HEADERS: '', OTEL_EXPORTER_OTLP_METRICS_HEADERS: '', OTEL_EXPORTER_OTLP_TRACES_HEADERS: '', OTELC_REPORT_PATH: report });
        assert.equal(baseline.status, 0, baseline.stderr); assert.equal(baseline.stdout, '48\n'); assert.deepEqual(measured, baseline);
        assert.deepEqual(fs.readFileSync(filename), original);
        const result = JSON.parse(fs.readFileSync(report)); assert.equal(result.lifetimes.admitted, 2); assert.equal(result.lifetimes.completed, 0); assert.equal(result.lifetimes.censored, 2);
        const inspected = await child(cli, ['--config', policy, '--language', 'typescript', 'inspect', filename, '--json']);
        assert.equal(inspected.status, 0, inspected.stderr); const inventory = JSON.parse(inspected.stdout);
        assert.equal(inventory.types.length, 1); assert.ok(inventory.types[0].selected); assert.ok(inventory.types[0].line >= 1);
        assert.match(inventory.types[0].name, name.endsWith('.mts') ? /\.WorkItem$/ : /\.AnnotatedItem$/);
      }
    }
  } finally { await server.close(); fs.rmSync(directory, { recursive: true, force: true }); }
});
