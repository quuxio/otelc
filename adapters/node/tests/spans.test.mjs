import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Module } from 'node:module';
import { JsonTraceSerializer, ProtobufMetricsSerializer } from '@opentelemetry/otlp-transformer';
import { Runtime, RUNTIME } from '../runtime.mjs';
import { transform } from '../transform.mjs';
import { headers } from '../exporter.mjs';
import { plan, receiver, child } from './helpers.mjs';

export const tracePlan = endpoint => ({ ...plan(endpoint), traces: { enabled: true, root_sample_ratio: 1, max_active_traces: 8, max_spans_per_trace: 32 }, export: { ...plan(endpoint).export, max_queued_batches: 8 }, trace_export: { endpoint: endpoint.replace('/v1/metrics', '/v1/traces'), protocol: 'http/protobuf', timeout_ms: 300 } });
function compile(source, runtime) {
  const module = new Module('/tmp/span-shapes.cjs');
  module._compile(transform(source, '/tmp/span-shapes.cjs', 'span-shapes.cjs', runtime.plan, runtime).code, '/tmp/span-shapes.cjs');
  return module.exports;
}
async function usingTraces(action, configure = () => {}) {
  const metrics = await receiver(), traces = await receiver();
  const p = tracePlan(metrics.endpoint); p.trace_export.endpoint = traces.endpoint.replace('/v1/metrics', '/v1/traces'); configure(p);
  const runtime = new Runtime(p), trees = [];
  const original = runtime.traces.exporter.export.bind(runtime.traces.exporter);
  runtime.traces.exporter.export = (spans, deadline) => {
    const data = JSON.parse(Buffer.from(JsonTraceSerializer.serializeRequest(spans)).toString());
    trees.push(data.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans)));
    return original(spans, deadline);
  };
  globalThis[RUNTIME] = runtime;
  try { await action(runtime, trees, metrics, traces); }
  finally { await runtime.close(); delete globalThis[RUNTIME]; await metrics.close(); await traces.close(); }
}
const graph = tree => {
  const ids = new Set(tree.map(span => span.spanId));
  assert.equal(ids.size, tree.length);
  assert.equal(new Set(tree.map(span => span.traceId)).size, 1);
  assert.ok(tree.every(span => /^[0-9a-f]{32}$/.test(span.traceId) && /^[0-9a-f]{16}$/.test(span.spanId)));
  assert.equal(tree.filter(span => !span.parentSpanId).length, 1);
  assert.ok(tree.every(span => !span.parentSpanId || ids.has(span.parentSpanId)));
  assert.ok(tree.every(span => BigInt(span.endTimeUnixNano) >= BigInt(span.startTimeUnixNano)));
};
test('configured spans export SDK protobuf independently from metrics', async () => {
  const server = await receiver(); const runtime = new Runtime(tracePlan(server.endpoint));
  try {
    runtime.exit(runtime.enter('root'), false);
    await runtime.close();
    assert.equal(runtime.report().traces?.completed_trees, 1);
    assert.equal(runtime.report().export_loss, 0);
    assert.ok(server.requests.some(request => request.url === '/v1/traces' && request.body.length > 0));
  } finally { await runtime.close(); await server.close(); }
});
test('SDK trees preserve recursion, objects and Throwable identity without application payloads', async () => usingTraces(async (runtime, trees) => {
  const api = compile(`function recursive(n){return n ? recursive(n-1)+1 : 0;}
    function child(value){return value;} function parent(value){return child(value);}
    function escaping(error){throw error;} function caught(){try{throw new Error('private');}catch{return 7;}}
    module.exports={recursive,parent,escaping,caught};`, runtime);
  const value = {}, error = new Error('original private payload');
  assert.equal(api.recursive(3), 3); assert.equal(api.parent(value), value);
  assert.throws(() => api.escaping(error), e => e === error); assert.equal(api.caught(), 7);
  await runtime.close(); assert.deepEqual(trees.map(tree => tree.length).sort(), [1,1,2,4]); trees.forEach(graph);
  assert.equal(trees.flat().filter(span => span.status?.code === 2).length, 1);
  assert.ok(!JSON.stringify(trees).includes(error.message)); assert.equal(runtime.current, null);
  assert.equal(runtime.report().export_loss, 0); assert.deepEqual(runtime.report().traces.losses, {});
}));
test('direct awaits, rejected awaits and finally restore private parenting', async () => usingTraces(async (runtime, trees) => {
  const api = compile(`function child(){return 42;}
    async function asyncChild(){await Promise.resolve();return child();}
    async function parent(){await asyncChild();return child();}
    async function rejected(error){await Promise.reject(error);}
    async function caught(error){try{await rejected(error);}catch(original){if(original!==error)throw new Error('identity');child();}finally{child();}}
    async function bare(value){return value;}
    module.exports={parent,caught,bare};`, runtime);
  assert.equal(await api.parent(), 42);
  await api.caught(new Error('private rejection'));
  const value = {}; assert.equal(await api.bare(Promise.resolve(value)), value);
  await runtime.close(); assert.deepEqual(trees.map(tree => tree.length).sort(), [1,4,4]); trees.forEach(graph);
  const caught = trees.find(tree => tree.some(span => span.name.endsWith('.caught')));
  const root = caught.find(span => span.name.endsWith('.caught'));
  assert.equal(root.status?.code, 0); assert.ok(caught.filter(span => span !== root).every(span => span.parentSpanId === root.spanId));
  assert.equal(runtime.current, null); assert.equal(runtime.report().function_calls, 9);
  assert.equal(runtime.report().export_loss, 0); assert.deepEqual(runtime.report().traces.losses, {});
}));
test('nested await operands detach at every suspension and do not parent unrelated work', async () => usingTraces(async (runtime, trees) => {
  const api = compile('async function nested(value){return await (await value);}function independent(){return 7;}module.exports={nested,independent};', runtime);
  let resolve; const pending = api.nested(new Promise(done => { resolve = done; }));
  assert.equal(runtime.current, null, 'inner await left the selected scope attached');
  assert.equal(api.independent(), 7); resolve(42); assert.equal(await pending, 42);
  await runtime.close(); assert.deepEqual(trees.map(tree => tree.length), [1,1]); trees.forEach(graph);
}));
test('catch-pattern getters execute under the resumed selected scope', async () => usingTraces(async (runtime, trees) => {
  const api = compile('function child(){return 42;}const error={get answer(){return child();}};async function parent(error){try{await Promise.reject(error);}catch({answer}){return answer;}}module.exports={parent,error};', runtime);
  assert.equal(await api.parent(api.error), 42); await runtime.close();
  assert.deepEqual(trees.map(tree => tree.length), [3]); graph(trees[0]);
}));
test('trace-enabled transforms preserve thenable reads, microtasks and rejection event identities', async () => {
  const source = `async function selected(value,events){events.push('body');return value;}
    async function awaits(value,events){events.push('before');const result=await value;events.push('after');return result;}
    module.exports={selected,awaits};`;
  const script = `import {Module} from 'node:module';import {Runtime,RUNTIME} from './adapters/node/runtime.mjs';import {transform} from './adapters/node/transform.mjs';
    const runtime=new Runtime(${JSON.stringify(tracePlan('http://127.0.0.1:1/v1/metrics'))});globalThis[RUNTIME]=runtime;
    const source=${JSON.stringify(source)},module=new Module('/tmp/contracts.cjs');
    module._compile(process.argv.at(-1)==='instrumented'?transform(source,'/tmp/contracts.cjs','contracts.cjs',runtime.plan,runtime).code:source,'/tmp/contracts.cjs');
    const events=[], value={}, thenable={get then(){events.push('get');return function(resolve){events.push(this===thenable?'this':'wrong-this');resolve(value);};}};
    queueMicrotask(()=>events.push('micro-before'));const promise=module.exports.selected(thenable,events);events.push('returned');queueMicrotask(()=>events.push('micro-after'));
    events.push(await promise===value);events.push(await module.exports.awaits(Promise.resolve(value),events)===value);
    const failure=new Error('original');let rejected;
    process.on('unhandledRejection',(error,p)=>{events.push(['unhandled',error===failure,p===rejected]);setImmediate(()=>p.catch(e=>events.push(['caught',e===failure])));});
    process.on('rejectionHandled',p=>events.push(['handled',p===rejected]));
    rejected=module.exports.selected(Promise.reject(failure),events);
    await new Promise(resolve=>setTimeout(resolve,40));console.log(JSON.stringify(events));await runtime.close();`;
  const [plain, instrumented] = await Promise.all([child(process.execPath, ['--input-type=module','-e',script,'plain']), child(process.execPath, ['--input-type=module','-e',script,'instrumented'])]);
  assert.equal(plain.status, 0, plain.stderr); assert.deepEqual(instrumented, plain);
  const events = JSON.parse(instrumented.stdout);
  assert.equal(events.filter(event => event === 'get').length, 1);
  assert.ok(events.some(event => Array.isArray(event) && event[0] === 'unhandled' && event[1] && event[2]));
  assert.ok(events.some(event => Array.isArray(event) && event[0] === 'handled' && event[1]));
});
test('unsupported implicit suspension constructs fail visibly only when selected with traces', async () => usingTraces(async runtime => {
  for (const source of ['async function* selected(){yield 1;}','function* selected(){yield* [1];}','async function selected(values){for await(const value of values){} }','function selected(value){with(value){return answer;}}','with({}){function selected(){return answer;}}']) {
    assert.throws(() => compile(source, runtime), /with traces are not qualified/);
  }
  runtime.plan.function_matchers.include = ['(?-u)nothing'];
  assert.doesNotThrow(() => compile('async function* ignored(){yield 1;}', runtime));
}));
test('shared function, active-call and span limits discard complete trees and suppress rejected scopes', async () => {
  for (const setting of ['max_functions','max_active_calls','max_spans_per_trace']) {
    await usingTraces(async (runtime, trees) => {
      const api = compile('function child(){return 42;}function root(){return child();}module.exports={root,child};', runtime);
      assert.equal(api.root(), 42); await runtime.close(); assert.deepEqual(trees, []);
      const reason = setting === 'max_functions' ? 'function_capacity' : setting === 'max_active_calls' ? 'active_call_capacity' : 'span_capacity';
      assert.deepEqual(runtime.report().traces.losses, { [reason]: 1 }); assert.equal(runtime.current, null);
    }, p => { (setting === 'max_spans_per_trace' ? p.traces : p.runtime)[setting] = 1; });
  }
  await usingTraces(async (runtime, trees) => {
    runtime.register('span-shapes.child');
    const api = compile('function child(){return 42;}function root(){return child();}module.exports={root,child};', runtime);
    assert.equal(api.root(), 42); assert.equal(api.child(), 42); await runtime.close();
    assert.deepEqual(trees.map(tree => tree.map(span => span.name)), [['span-shapes.child']]);
    assert.deepEqual(runtime.report().traces.losses, { function_capacity: 1 });
  }, p => { p.runtime.max_functions = 1; });
});
test('sampling zero and metrics-disabled tracing preserve admission and bounded incomplete reporting', async () => {
  await usingTraces(async (runtime, trees) => {
    const api = compile('function root(){return child();}function child(){return 42;}module.exports=root;', runtime);
    assert.equal(api(), 42); await runtime.close(); assert.deepEqual(trees, []);
    assert.equal(runtime.report().traces.sampled_out_roots, 1);
    assert.equal(runtime.report().losses.incomplete, 0); assert.deepEqual(runtime.report().traces.losses, {});
  }, p => { p.traces.root_sample_ratio = 0; p.metrics.enabled = false; });
  await usingTraces(async (runtime, trees) => {
    const api = compile('async function root(value){return await value;}module.exports=root;', runtime);
    let resolve; const pending = api(new Promise(done => { resolve = done; }));
    runtime.enabled = true; resolve(42); assert.equal(await pending, 42); await runtime.close();
    assert.equal(runtime.report().function_calls, 0); assert.equal(trees.length, 1); graph(trees[0]);
  }, p => { p.metrics.enabled = false; });
});
test('root, queue and incomplete capacities are bounded and reusable', async () => {
  await usingTraces(async (runtime, trees) => {
    const api = compile('async function pending(value){return value;}function child(){return 42;}module.exports={pending,child};', runtime);
    api.pending(new Promise(() => {})); assert.equal(api.child(), 42); await runtime.close();
    assert.deepEqual(trees, []); assert.deepEqual(runtime.report().traces.losses, { trace_capacity: 1, incomplete: 1 });
  }, p => { p.traces.max_active_traces = 1; });
  await usingTraces(async (runtime, trees) => {
    const api = compile('function child(){return 42;}module.exports=child;', runtime);
    assert.equal(api(), 42); assert.equal(api(), 42); await runtime.close();
    assert.equal(trees.length, 1); assert.deepEqual(runtime.report().traces.losses, { queue_capacity: 1 });
  }, p => { p.export.max_queued_batches = 1; p.export.interval_ms = 60000; });
});
test('generator suspension, throw and return restore the actual current caller', async () => usingTraces(async (runtime, trees) => {
  const api = compile(`function child(){return 42;}function* generator(){try{yield child();yield child();}finally{child();}}
    function resume(g){return g.next();}function stop(g){return g.return(7);}module.exports={generator,resume,stop};`, runtime);
  const first = api.generator(); assert.deepEqual(api.resume(first), {value:42,done:false});
  assert.equal(runtime.current, null); assert.deepEqual(api.stop(first), {value:7,done:true});
  assert.equal(runtime.current, null);
  const second = api.generator(); second.next(); const error = new Error('identity'); assert.throws(() => second.throw(error), e => e === error);
  await runtime.close(); trees.forEach(graph);
  assert.deepEqual(runtime.report().traces.losses, {});
}));
test('transient trace retries reuse the payload and permanent or malformed acknowledgements are terminal', async () => {
  for (const kind of ['retry','permanent','partial','malformed','oversized','202','redirect','stalled-error']) {
    let attempts = 0;
    const metrics = await receiver(), traces = await receiver(response => {
      attempts++;
      if (kind === 'retry' && attempts === 1) { response.writeHead(503); response.end(); }
      else if (kind === 'permanent') { response.writeHead(401); response.end(); }
      else if (kind === 'partial') response.end(Buffer.from([10,2,8,1]));
      else if (kind === 'malformed') response.end(Buffer.from([255]));
      else if (kind === 'oversized') response.end(Buffer.alloc(65537));
      else if (kind === '202') { response.writeHead(202); response.end(); }
      else if (kind === 'redirect') { response.writeHead(302,{Location:metrics.endpoint}); response.end(); }
      else if (kind === 'stalled-error') { response.writeHead(401); response.flushHeaders(); }
      else response.end();
    });
    const p = tracePlan(metrics.endpoint); p.trace_export.endpoint = traces.endpoint;
    const runtime = new Runtime(p);
    try {
      runtime.exit(runtime.enter('one'), false); await runtime.close();
      assert.equal(attempts, kind === 'retry' ? 2 : 1, kind);
      assert.equal(runtime.report().export_loss, kind === 'retry' ? 0 : 1, kind);
      if (kind === 'retry') assert.deepEqual(traces.requests[0].body,traces.requests[1].body);
      assert.equal(runtime.report().export_finished, true, kind);
    } finally { await runtime.close(); await metrics.close(); await traces.close(); }
  }
});
test('a short metrics reader deadline does not truncate the independent trace phase', async () => {
  let acknowledged = false;
  const metrics = await receiver(), traces = await receiver(response => setTimeout(() => { acknowledged = true; response.end(); }, 250));
  const p = tracePlan(metrics.endpoint); p.export.interval_ms = 100; p.export.timeout_ms = 100; p.trace_export.timeout_ms = 1000; p.runtime.shutdown_timeout_ms = 2000; p.trace_export.endpoint = traces.endpoint;
  const runtime = new Runtime(p);
  try {
    runtime.exit(runtime.enter('one'), false); await runtime.close();
    assert.equal(acknowledged, true, 'shutdown reported completion before the trace acknowledgement');
    assert.equal(runtime.report().export_loss, 0); assert.equal(runtime.report().export_finished, true);
    assert.equal(traces.requests.length, 1);
  } finally { await runtime.close(); await metrics.close(); await traces.close(); }
});
test('resolved trace policy rejects forged types and unsafe settings with generic errors', () => {
  const changes = [
    p => { p.traces.enabled = 'true'; },
    p => { p.traces.root_sample_ratio = '0.5'; }, p => { p.traces.root_sample_ratio = NaN; }, p => { p.traces.root_sample_ratio = 2; },
    ...['max_active_traces','max_spans_per_trace'].flatMap(key => [p => { p.traces[key] = 0; }, p => { p.traces[key] = 1.5; }, p => { p.traces[key] = '1'; }]),
    p => { p.traces.max_active_traces = 65536; p.traces.max_spans_per_trace = 65536; },
    p => { p.export.max_queued_batches = 65; }, p => { p.trace_export.timeout_ms = 60001; }, p => { p.trace_export.protocol = 'grpc'; },
    ...['http://private.example/secret', 'https://user:secret@example.invalid', 'https://example.invalid?private=value', 'https://example.invalid#private', 'https://example.invalid:65536'].map(endpoint => p => { p.trace_export.endpoint = endpoint; })
  ];
  for (const change of changes) {
    const p = tracePlan('http://127.0.0.1:1/v1/metrics'); change(p);
    assert.throws(() => new Runtime(p), { message: 'invalid resolved JavaScript trace settings' });
  }
  assert.deepEqual(headers({ OTEL_EXPORTER_OTLP_HEADERS: 'x-private=ignored', OTEL_EXPORTER_OTLP_TRACES_HEADERS: '' }, 'OTEL_EXPORTER_OTLP_TRACES_HEADERS'), {});
  assert.throws(() => headers({ OTEL_EXPORTER_OTLP_HEADERS: 'x='+'s'.repeat(8192) }), /8192/);
});
test('stalled traces respect the shared shutdown budget and report unfinished export', async () => {
  let seen;
  const received = new Promise(resolve => { seen = resolve; });
  const metrics = await receiver(), traces = await receiver(() => seen());
  const p = tracePlan(metrics.endpoint); p.trace_export.endpoint = traces.endpoint; p.trace_export.timeout_ms = 60000; p.runtime.shutdown_timeout_ms = 150;
  const runtime = new Runtime(p);
  try {
    runtime.exit(runtime.enter('one'), false);
    await runtime.provider.forceFlush(); await received;
    const started = performance.now(); await runtime.close();
    assert.ok(performance.now()-started < 1000); assert.equal(runtime.report().export_finished, false);
    assert.ok(runtime.report().export_loss >= 1); assert.equal(traces.requests.length, 1);
  } finally { await runtime.close(); await metrics.close(); await traces.close(); }
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(runtime.report().export_finished, false, 'late cancellation changed a timed-out shutdown to verified completion');
});
test('late SDK completion cannot change a timed-out shutdown into verified completion', async () => {
  const server = await receiver(), p = tracePlan(server.endpoint); p.export.interval_ms = 60000; p.runtime.shutdown_timeout_ms = 150;
  const runtime = new Runtime(p), shutdown = runtime.provider.shutdown.bind(runtime.provider);
  let release; const pending = new Promise(resolve => { release = resolve; });
  runtime.provider.shutdown = () => pending;
  try {
    await runtime.close(); assert.equal(runtime.report().export_finished, false);
    release(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(runtime.report().export_finished, false, 'SDK completed after the shared deadline');
  } finally { release(); runtime.provider.shutdown = shutdown; await shutdown(); await server.close(); }
});
test('oversized completed payloads are discarded before SDK serialization', async () => usingTraces(async (runtime, trees) => {
  const root = runtime.enter('root'), previous = runtime.attach(root);
  const name = 'x'.repeat(1024);
  for (let n = 0; n < 8192; n++) runtime.exit(runtime.enter(name), false);
  runtime.detach(previous); runtime.exit(root, false); await runtime.close();
  assert.deepEqual(trees, []); assert.deepEqual(runtime.report().traces.losses, { batch_bytes: 1 });
}, p => { p.traces.max_spans_per_trace = 16384; }));
test('overlapping async calls keep separate SDK roots and settlement status', async () => usingTraces(async (runtime, trees) => {
  const selected = compile('module.exports=async function selected(value){return value;};', runtime);
  const completions = [], promises = [];
  for (let n = 0; n < 5; n++) promises.push(selected(new Promise(resolve => completions.push(resolve))));
  assert.equal(runtime.current, null);
  completions.forEach((resolve, n) => resolve(n)); assert.deepEqual(await Promise.all(promises), [0,1,2,3,4]);
  const error = new Error('identity'); await assert.rejects(selected(Promise.reject(error)), e => e === error);
  await runtime.close(); assert.deepEqual(trees.map(tree => tree.length), [1,1,1,1,1,1]); trees.forEach(graph);
  assert.equal(new Set(trees.map(tree => tree[0].traceId)).size, 6); assert.equal(trees.flat().filter(span => span.status?.code === 2).length, 1);
  assert.deepEqual(runtime.report().traces.losses, {});
}));
test('constructors and private methods preserve this, new.target, parameters and exceptions', async () => usingTraces(async (runtime, trees) => {
  const Example = compile(`class Base{constructor(value){this.value=value;}}class Example extends Base{
    constructor(value){super(value);if(value<0)throw new Error('original');this.target=new.target;}
    #child(globalThis,Symbol,process){return this.value+globalThis+Symbol+process;}
    parent(){return this.#child(1,2,3);}}
    module.exports=Example;`, runtime);
  const value = new Example(7); assert.equal(value.target, Example); assert.equal(value.parent(), 13);
  assert.throws(() => new Example(-1), /original/); await runtime.close(); trees.forEach(graph);
  assert.equal(trees.flat().filter(span => span.status?.code === 2).length, 1); assert.deepEqual(runtime.report().traces.losses, {});
}));
test('trace export loss changes the next real metric snapshot without new application calls', async () => {
  const metrics = await receiver(), traces = await receiver(response => { response.writeHead(401); response.end(); });
  const p = tracePlan(metrics.endpoint); p.trace_export.endpoint = traces.endpoint; p.export.interval_ms = 60000;
  const runtime = new Runtime(p);
  try {
    runtime.exit(runtime.enter('one'), false);
    await runtime.provider.forceFlush(); await runtime.traces.flush();
    assert.equal(runtime.report().export_loss, 1); const previous = metrics.requests.length;
    const snapshot = await runtime.reader.collect();
    const health = snapshot.resourceMetrics.scopeMetrics.flatMap(scope => scope.metrics).find(metric => metric.descriptor.name === 'otelc.export.dropped_batches');
    assert.equal(health.dataPoints[0].value, 1);
    await runtime.provider.forceFlush(); assert.equal(metrics.requests.length, previous+1); assert.equal(traces.requests.length, 1);
    assert.equal(runtime.report().function_calls, 1);
  } finally { await runtime.close(); await metrics.close(); await traces.close(); }
});
test('final trace failure reaches an acknowledged shutdown health metric without new calls', async () => {
  const metrics = await receiver(), traces = await receiver(response => { response.writeHead(401); response.end(); });
  const p = tracePlan(metrics.endpoint); p.trace_export.endpoint = traces.endpoint; p.export.interval_ms = 60000;
  const runtime = new Runtime(p), failedSnapshots = [];
  const original = runtime.exporter.export.bind(runtime.exporter);
  runtime.exporter.export = (data, callback) => {
    const health = data.scopeMetrics.flatMap(scope => scope.metrics).find(metric => metric.descriptor.name === 'otelc.export.dropped_batches');
    if (health.dataPoints[0].value === 1) failedSnapshots.push(Buffer.from(ProtobufMetricsSerializer.serializeRequest(data)));
    original(data, callback);
  };
  try {
    runtime.exit(runtime.enter('one'), false);
    const report = await runtime.close();
    assert.equal(report.export_loss, 1); assert.equal(report.export_finished, true);
    assert.equal(traces.requests.length, 1); assert.equal(report.function_calls, 1);
    assert.ok(metrics.requests.some(request => failedSnapshots.some(body => body.equals(request.body))), 'no acknowledged final metric contained the trace failure');
  } finally { await runtime.close(); await metrics.close(); await traces.close(); }
});
