import assert from 'node:assert/strict';
import { Module } from 'node:module';
import { AsyncLocalStorage } from 'node:async_hooks';
import { promiseHooks } from 'node:v8';
import { test } from 'node:test';
import { Runtime, RUNTIME } from '../runtime.mjs';
import { transform } from '../transform.mjs';
import { nativeObserver } from '../promise-observer.mjs';
import { plan, receiver, child } from './helpers.mjs';

function compile(source, runtime, filename = '/tmp/promise-shapes.cjs') {
  const output = transform(source, filename, filename.split('/').at(-1), runtime.plan, runtime);
  const module = new Module(filename); module._compile(output.code, filename);
  return module.exports;
}
async function usingRuntime(action, configure = () => {}) {
  const server = await receiver(); const p = plan(server.endpoint); configure(p);
  const runtime = new Runtime(p); globalThis[RUNTIME] = runtime;
  try { await action(runtime); }
  finally { await runtime.close(); delete globalThis[RUNTIME]; await server.close(); }
}
const zeroLoss = runtime => assert.ok(Object.values(runtime.report().losses).every(value => value === 0), JSON.stringify(runtime.report().losses));

test('public V8 observer reads all states without then access and captures bounded raw frames', async () => {
  const native = nativeObserver();
  assert.throws(() => native.state(), /expected a Promise/);
  assert.throws(() => native.state({ then() {} }), /expected a Promise/);
  const pending = new Promise(() => {}), fulfilled = Promise.resolve(7), rejected = Promise.reject(8);
  rejected.catch(() => {});
  Object.defineProperty(fulfilled, 'then', { get() { throw new Error('must not read then'); } });
  assert.equal(native.state(pending), 0); assert.equal(native.state(fulfilled), 1); assert.equal(native.state(rejected), 2);
  const frames = native.frames();
  assert.ok(frames.length > 0 && frames.length < 128);
  assert.ok(frames.some(frame => frame[0].endsWith('promise.test.mjs') && frame[2] > 0 && frame[3] > 0));
  function deep(n) { return n ? deep(n - 1) : native.frames(); }
  assert.equal(deep(150).length, 128);
});

test('async origins distinguish default parameter Promises, recursive defaults, arrows and methods', async () => usingRuntime(async runtime => {
  const api = compile(`async function selected(value=Promise.resolve(7)) { return value; }
    async function recursive(n, value=n ? recursive(n-1) : Promise.resolve(0)) { return (await value)+1; }
    const arrow=async value=>value;
    const object={ async ['method'](value) { return value; } };
    class Example { async #work(value) { return value; } async run(value) { return this.#work(value); } static async work(value) { return value; } }
    module.exports={selected,recursive,arrow,object,Example};`, runtime);
  assert.equal(api.selected.length, 0); assert.equal(api.selected.name, 'selected');
  assert.equal(await api.selected(), 7); assert.equal(await api.recursive(3), 4);
  assert.equal(await api.arrow(8), 8); assert.equal(await api.object.method(9), 9);
  assert.equal(await new api.Example().run(10), 10); assert.equal(await api.Example.work(11), 11);
  assert.equal(runtime.report().function_calls, 10); // The implicit constructor has no parsed body.
  zeroLoss(runtime);
}));

test('overlapping calls at one caller site retain distinct completion and error identities', async () => usingRuntime(async runtime => {
  const selected = compile('module.exports=async function selected(promise) { return promise; };', runtime);
  const done = [], promises = [];
  for (let n = 0; n < 5; n++) promises.push(selected(new Promise((resolve, reject) => done.push([resolve, reject]))));
  assert.equal(runtime.pending.size, 5);
  for (let n = 0; n < 5; n++) done[n][0](n);
  assert.deepEqual(await Promise.all(promises), [0, 1, 2, 3, 4]);
  assert.equal(runtime.report().function_calls, 5);
  const failure = new Error('original identity');
  await assert.rejects(selected(Promise.reject(failure)), value => value === failure);
  assert.equal(runtime.report().functions['promise-shapes.selected'].unwinds, 1);
  zeroLoss(runtime);
}));

test('async timing coexists with Promise hooks and AsyncLocalStorage without observer microtasks', async () => usingRuntime(async runtime => {
  let initialised = 0; const stop = promiseHooks.createHook({ init() { initialised++; } });
  const storage = new AsyncLocalStorage();
  try {
    const selected = compile('module.exports=async function selected(storage, events) { events.push(storage.getStore()); await Promise.resolve(); events.push(storage.getStore()); return 7; };', runtime);
    const events = [];
    await storage.run('request-context', async () => assert.equal(await selected(storage, events), 7));
    assert.deepEqual(events, ['request-context', 'request-context']);
    assert.ok(initialised > 0); assert.equal(runtime.report().function_calls, 1); zeroLoss(runtime);
  } finally { stop(); storage.disable(); }
}));

test('toggle admission keeps existing async calls and excludes calls begun while disabled', async () => usingRuntime(async runtime => {
  const selected = compile('module.exports=async function selected(value) { return value; };', runtime);
  let resolve; const admitted = selected(new Promise(done => { resolve = done; }));
  runtime.enabled = false; assert.equal(await selected(2), 2);
  resolve(1); assert.equal(await admitted, 1); assert.equal(runtime.report().function_calls, 1);
  runtime.enabled = true; assert.equal(await selected(3), 3);
  assert.equal(runtime.report().function_calls, 2); zeroLoss(runtime);
}));

test('shutdown finalises already-settled Promises and reports unresolved calls as incomplete', async () => usingRuntime(async runtime => {
  const selected = compile('module.exports=async function selected(value) { return value; };', runtime);
  selected(7); selected(new Promise(() => {}));
  const report = await runtime.close();
  assert.equal(report.function_calls, 1); assert.equal(report.losses.incomplete, 1);
  assert.equal(runtime.promiseObserver.roots.size, 0); assert.equal(runtime.promiseObserver.active.size, 0);
  assert.equal(await selected(8), 8); assert.equal(runtime.report().function_calls, 1);
}));

test('capacity and unidentifiable script origins produce visible losses and never alter returns', async () => usingRuntime(async runtime => {
  const selected = compile('module.exports=async function selected(value) { return value; };', runtime);
  let resolve; const first = selected(new Promise(done => { resolve = done; }));
  assert.equal(await selected(8), 8); assert.equal(runtime.report().losses.active_call_capacity, 1);
  resolve(7); assert.equal(await first, 7); assert.equal(runtime.report().function_calls, 1);
  const anonymous = new Function('require', 'module', transform('module.exports=async function unknown(){return 9;}', '/tmp/anonymous.cjs', 'anonymous.cjs', runtime.plan, runtime).code);
  const module = { exports: {} }; const { createRequire } = await import('node:module'); anonymous(createRequire(import.meta.url), module);
  assert.equal(await module.exports(), 9); assert.equal(runtime.report().losses.async_origin, 1);
}, p => { p.runtime.max_active_calls = 1; }));

test('observation does not suppress unhandledRejection or change rejectionHandled ordering', async () => {
  const base = `import { Module } from 'node:module'; import { Runtime,RUNTIME } from './adapters/node/runtime.mjs'; import { transform } from './adapters/node/transform.mjs';
    const runtime=new Runtime(${JSON.stringify(plan('http://127.0.0.1:1/v1/metrics'))}); globalThis[RUNTIME]=runtime;
    const source='module.exports=async function selected(value){return value;}';
    const code=process.argv.at(-1)==='instrumented' ? transform(source,'/tmp/unhandled.cjs','unhandled.cjs',runtime.plan,runtime).code : source;
    const module=new Module('/tmp/unhandled.cjs'); module._compile(code,'/tmp/unhandled.cjs');
    const events=[]; let promise; process.on('unhandledRejection',(error,p)=>{events.push(['unhandled',error.message,p===promise]); setImmediate(()=>p.catch(()=>{}));});
    process.on('rejectionHandled',p=>events.push(['handled',p===promise]));
    promise=module.exports(Promise.reject(new Error('original')));
    setTimeout(async()=>{console.log(JSON.stringify(events)); await runtime.close();},30);`;
  const original = await child(process.execPath, ['--input-type=module', '-e', base, 'original']);
  const observed = await child(process.execPath, ['--input-type=module', '-e', base, 'instrumented']);
  assert.equal(original.status, 0, original.stderr); assert.equal(observed.status, 0, observed.stderr);
  assert.deepEqual(JSON.parse(observed.stdout), [['unhandled', 'original', true], ['handled', true]]);
  assert.equal(observed.stdout, original.stdout);
});

test('settled Promise accounting survives GC before the next snapshot', async () => {
  const code = `import { Module } from 'node:module'; import { Runtime,RUNTIME } from './adapters/node/runtime.mjs'; import { transform } from './adapters/node/transform.mjs';
    const runtime=new Runtime(${JSON.stringify(plan('http://127.0.0.1:1/v1/metrics'))}); globalThis[RUNTIME]=runtime;
    const source='module.exports=async function selected(){return {value:7};}';
    const output=transform(source,'/tmp/gc.cjs','gc.cjs',runtime.plan,runtime); const module=new Module('/tmp/gc.cjs'); module._compile(output.code,'/tmp/gc.cjs');
    await module.exports(); await new Promise(resolve=>setImmediate(resolve)); global.gc();
    const report=runtime.report(); console.log(JSON.stringify({calls:report.function_calls,losses:report.losses})); await runtime.close();`;
  const result = await child(process.execPath, ['--expose-gc', '--input-type=module', '-e', code]);
  assert.equal(result.status, 0, result.stderr);
  const report = JSON.parse(result.stdout); assert.equal(report.calls, 1);
  assert.ok(Object.values(report.losses).every(value => value === 0));
});

test('deep origins, origin capacity and observer faults are bounded and visible', async () => usingRuntime(async runtime => {
  const selected = compile('module.exports=async function selected(){return 7;};', runtime);
  function deep(n) { return n ? deep(n - 1) : selected(); }
  assert.equal(await deep(150), 7); assert.equal(runtime.report().losses.async_origin, 1);
  const observer = runtime.promiseObserver;
  const sites = [...observer.sites.values()];
  observer.register(Array.from({ length: runtime.plan.runtime.max_functions + 1 }, (_, n) => ({ ...sites[0], id: n + 10000 })));
  assert.equal(observer.sites.size, runtime.plan.runtime.max_functions);
  assert.equal(runtime.report().losses.function_capacity, 2);
  const frames = observer.native.frames;
  try {
    observer.native.frames = () => { throw new Error('observer fault'); };
    assert.equal(await selected(), 7);
    assert.ok(runtime.report().losses.invalid >= 2);
  } finally { observer.native.frames = frames; }
}));

test('missing addon fails explicitly instead of falling back to return rewriting', () => {
  const previous = process.env.OTELC_NODE_OBSERVER;
  try {
    process.env.OTELC_NODE_OBSERVER = '/does-not-exist/otelc_node_observer.node';
    assert.throws(nativeObserver, /Build the Promise observer/);
  } finally {
    if (previous === undefined) delete process.env.OTELC_NODE_OBSERVER;
    else process.env.OTELC_NODE_OBSERVER = previous;
  }
});
