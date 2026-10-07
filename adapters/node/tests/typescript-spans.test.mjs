import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { test } from 'node:test';
import { JsonTraceSerializer } from '@opentelemetry/otlp-transformer';
import { install } from '../register.mjs';
import { plan, receiver, child } from './helpers.mjs';

async function typed(action, { include = ['(?-u).*'], target = 'ES2022' } = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-typed-traces-'));
  fs.writeFileSync(path.join(directory, 'tsconfig.json'), JSON.stringify({ compilerOptions: { target } }));
  const metrics = await receiver(), traces = await receiver();
  const p = { ...plan(metrics.endpoint), language: 'typescript',
    source_matchers: { include, exclude: [] }, export: { ...plan(metrics.endpoint).export, max_queued_batches: 8 },
    traces: { enabled: true, root_sample_ratio: 1, max_active_traces: 8, max_spans_per_trace: 32 },
    trace_export: { endpoint: traces.endpoint, protocol: 'http/protobuf', timeout_ms: 300 } };
  const application = await install(p, directory), trees = [];
  const original = application.runtime.traces.exporter.export.bind(application.runtime.traces.exporter);
  application.runtime.traces.exporter.export = (spans, deadline) => {
    const data = JSON.parse(Buffer.from(JsonTraceSerializer.serializeRequest(spans)).toString());
    trees.push(data.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans)));
    return original(spans, deadline);
  };
  const write = (filename, source) => { const filenamePath = path.join(directory, filename); fs.writeFileSync(filenamePath, source); return filenamePath; };
  try { await action(application, trees, write, metrics, traces); }
  finally { await application.close(); await metrics.close(); await traces.close(); fs.rmSync(directory, { recursive: true }); }
}

test('trace-enabled typed imports outside source selection are compiled without probes', async () => typed(async (application, trees, write) => {
  const helper = write('helper.mts', 'export enum Values { Factor=2 }\n// otelc.unknown\nexport function helper(n:number):number{return n*Values.Factor;}');
  const source = "import {helper} from './helper.mts';\nexport function parent(n:number):number{return helper(n)+1;}";
  const filename = write('app.mts', source);
  const api = await import(pathToFileURL(filename).href);
  assert.equal(api.parent(4), 9);
  await application.close();
  assert.equal(application.runtime.report().functions['helper.helper'], undefined);
  assert.deepEqual(trees.map(tree => tree.map(span => span.name)), [['app.parent']]);
  assert.equal(fs.readFileSync(filename, 'utf8'), source); assert.match(fs.readFileSync(helper, 'utf8'), /otelc.unknown/);
}, { include: ['(?-u)app\\.mts'] }));

for (const target of ['ES2018', 'ES2022']) test(`typed ${target} emission preserves original names, decorators and async trees`, async () => typed(async (application, trees, write) => {
  const source = `enum State{Open=4}namespace Rules{export function bonus(n:number):number{return n+1;}}
    function decorate(value:any,context:any){return value;}
    export class Order{constructor(public value:number){} @decorate method<T extends number>(n:T):number{return Rules.bonus(n)+this.value;}}
    export function recursive(n:number):number{return n?recursive(n-1)+1:0;}
    export function child<T>(value:T):T{return value;}
    export async function asyncChild<T>(value:T):Promise<T>{await Promise.resolve();return child(value);}
    export async function asyncParent<T>(value:T):Promise<T>{return asyncChild(value);}
    export async function escaping(error:Error):Promise<never>{await Promise.resolve();throw error;}
    export const initial=State.Open;`;
  const filename = write('app.mts', source), api = await import(pathToFileURL(filename).href);
  const order = new api.Order(api.initial), value = {}, error = new Error('original private error');
  assert.equal(order.value, 4); assert.equal(order.method(3), 8); assert.equal(api.recursive(3), 3);
  assert.equal(await api.asyncParent(value), value); await assert.rejects(api.escaping(error), e => e === error);
  await application.close();
  const names = trees.flat().map(span => span.name);
  assert.ok(names.every(name => /^(app\.(decorate|Order\.(constructor|method)|Rules\.bonus|recursive|child|asyncChild|asyncParent|escaping))$/.test(name)), JSON.stringify(names));
  const asyncTree = trees.find(tree => tree.some(span => span.name === 'app.asyncParent'));
  assert.equal(asyncTree.length, 3); const parent = asyncTree.find(span => span.name === 'app.asyncParent'), awaited = asyncTree.find(span => span.name === 'app.asyncChild');
  assert.equal(awaited.parentSpanId, parent.spanId); assert.equal(asyncTree.find(span => span.name === 'app.child').parentSpanId, awaited.spanId);
  assert.equal(trees.find(tree => tree.some(span => span.name === 'app.recursive')).length, 4);
  assert.equal(trees.flat().filter(span => span.status?.code === 2).length, 1); assert.ok(!JSON.stringify(trees).includes(error.message));
  assert.equal(application.runtime.current, null); assert.equal(application.runtime.report().export_loss, 0); assert.ok(Object.values(application.runtime.report().losses).every(value => value === 0));
  assert.equal(fs.readFileSync(filename, 'utf8'), source);
}, { target }));

test('typed CommonJS annotations preserve exclusions, overloads and accessors', async () => typed(async (application, trees, write) => {
  application.runtime.plan.function_matchers.include = [];
  const source = `// otelc.instrument
    function selected(value:number):number;
    // otelc.instrument
    function selected(value:number):number{return value*3;}
    // otelc.exclude
    function excluded(value:number):number{return value-1;}
    class Order{constructor(public value:number){}
      // otelc.instrument
      get answer():number{return selected(this.value);}}
    export={selected,excluded,Order};`;
  const filename = write('app.cts', source), api = (await import(pathToFileURL(filename).href)).default;
  assert.equal(api.selected.length, 1); assert.equal(api.selected(4), 12); assert.equal(api.excluded(4), 3); assert.equal(new api.Order(4).answer, 12);
  await application.close();
  assert.deepEqual(trees.map(tree => tree.map(span => span.name)).sort(), [['app.Order.get answer','app.selected'],['app.selected']].sort());
  assert.equal(application.runtime.report().functions['app.excluded'], undefined); assert.equal(application.runtime.report().functions['app.Order.constructor'], undefined);
  assert.equal(fs.readFileSync(filename, 'utf8'), source);
}));

test('selected typed implicit suspension rejects clearly while excluded source stays runnable', async () => typed(async (application, trees, write) => {
  const filename = write('selected.mts', 'export async function* stream():AsyncGenerator<number>{yield 1;}');
  await assert.rejects(import(pathToFileURL(filename).href), /async generators with traces are not qualified/);
  const ignored = write('ignored.mts', 'export async function* stream():AsyncGenerator<number>{yield 1;}');
  const api = await import(pathToFileURL(ignored).href); assert.deepEqual(await api.stream().next(), {value:1,done:false});
  await application.close(); assert.deepEqual(trees, []);
}, { include:['(?-u)selected\\.mts'] }));

test('typed trace emission preserves thenable access, microtasks and rejection event identity', async () => {
  const p = { ...plan('http://127.0.0.1:1/v1/metrics'), language: 'typescript', export: { ...plan('http://127.0.0.1:1/v1/metrics').export, max_queued_batches: 8 },
    traces: { enabled: true, root_sample_ratio: 1, max_active_traces: 8, max_spans_per_trace: 32 },
    trace_export: { endpoint:'http://127.0.0.1:1/v1/traces',protocol:'http/protobuf',timeout_ms:100 } };
  const source = 'async function selected<T>(value:T,events:unknown[]):Promise<T>{events.push("body");return value;}async function awaits<T>(value:T,events:unknown[]):Promise<T>{events.push("before");const result=await value;events.push("after");return result;}module.exports={selected,awaits};';
  const script = `import {Module} from 'node:module';import {Runtime,RUNTIME} from './adapters/node/runtime.mjs';import {transform} from './adapters/node/transform.mjs';import {transpile} from './adapters/node/typescript.mjs';
    const runtime=new Runtime(${JSON.stringify(p)});globalThis[RUNTIME]=runtime;
    const prepared=transpile(${JSON.stringify(source)},'/tmp/typed-contracts.cts','typed-contracts.cts',runtime.plan),module=new Module('/tmp/typed-contracts.cts');
    module._compile(process.argv.at(-1)==='instrumented'?transform(prepared.code,'/tmp/typed-contracts.cts','typed-contracts.cts',runtime.plan,runtime,prepared).code:prepared.code,'/tmp/typed-contracts.cts');
    const events=[],value={},thenable={get then(){events.push('get');return function(resolve){events.push(this===thenable?'this':'wrong-this');resolve(value);};}};
    queueMicrotask(()=>events.push('micro-before'));const promise=module.exports.selected(thenable,events);events.push('returned');queueMicrotask(()=>events.push('micro-after'));
    events.push(await promise===value);events.push(await module.exports.awaits(Promise.resolve(value),events)===value);
    const failure=new Error('original');let rejected;
    process.on('unhandledRejection',(error,p)=>{events.push(['unhandled',error===failure,p===rejected]);setImmediate(()=>p.catch(e=>events.push(['caught',e===failure])));});
    process.on('rejectionHandled',p=>events.push(['handled',p===rejected]));rejected=module.exports.selected(Promise.reject(failure),events);
    await new Promise(resolve=>setTimeout(resolve,40));console.log(JSON.stringify(events));await runtime.close();`;
  const [plain, instrumented] = await Promise.all([child(process.execPath,['--input-type=module','-e',script,'plain']),child(process.execPath,['--input-type=module','-e',script,'instrumented'])]);
  assert.equal(plain.status,0,plain.stderr); assert.deepEqual(instrumented,plain);
  const events=JSON.parse(instrumented.stdout); assert.equal(events.filter(event=>event==='get').length,1);
  for(const name of ['unhandled','handled']) assert.ok(events.some(event=>Array.isArray(event)&&event[0]===name&&event.slice(1).every(Boolean)));
});
