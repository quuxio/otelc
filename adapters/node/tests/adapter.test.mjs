import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { test } from 'node:test';
import { Selection, sourceName } from '../policy.mjs';
import { transform, MARKER } from '../transform.mjs';
import { Runtime, RUNTIME } from '../runtime.mjs';
import { install } from '../register.mjs';
import { inspect } from '../cli.mjs';
import { headers } from '../exporter.mjs';
import { root, plan, receiver, control, child } from './helpers.mjs';

const require = createRequire(import.meta.url);

test('shutdown includes an idle control connection in its deadline', async () => {
  const r = await receiver(); const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-idle-'));
  fs.chmodSync(directory, 0o700);
  const p = plan(r.endpoint); p.runtime.control_socket = path.join(directory, 'metrics.sock'); p.runtime.shutdown_timeout_ms = 50;
  const runtime = new Runtime(p); let connection;
  try {
    await runtime.bindControl();
    connection = net.createConnection(p.runtime.control_socket); connection.on('error', () => {});
    await new Promise(resolve => connection.once('connect', resolve));
    const started = performance.now();
    await Promise.race([runtime.close(), new Promise((_, reject) => setTimeout(() => reject(new Error('shutdown exceeded 150ms')), 150))]);
    assert.ok(performance.now() - started < 150);
    assert.equal(fs.existsSync(p.runtime.control_socket), false);
  } finally { connection?.destroy(); await runtime.close(); await r.close(); fs.rmSync(directory, { recursive: true }); }
});

test('async completion follows returned Promise settlement and preserves user finally ordering', async () => {
  const r = await receiver(); const runtime = new Runtime(plan(r.endpoint)); globalThis[RUNTIME] = runtime;
  try {
    const source = `async function selected(promise, events) { try { return promise; } finally { events.push('finally'); } }
      async function overridden(promise) { try { return promise; } finally { return 7; } }
      module.exports = { selected, overridden };`;
    const output = transform(source, '/tmp/async.cjs', 'async.cjs', runtime.plan, runtime);
    const module = { exports: {} }; new Function('require', 'module', output.code)(require, module);
    const events = []; let resolve;
    const result = module.exports.selected(new Promise(done => { resolve = done; }), events);
    assert.deepEqual(events, ['finally']);
    assert.equal(runtime.report().function_calls, 0);
    await new Promise(done => setTimeout(done, 20)); resolve(5); assert.equal(await result, 5);
    assert.equal(runtime.report().function_calls, 1);
    const failure = new Error('same rejection'); let reject;
    const rejected = module.exports.selected(new Promise((_, fail) => { reject = fail; }), events);
    assert.equal(runtime.report().function_calls, 1); reject(failure);
    await assert.rejects(rejected, error => error === failure);
    assert.equal(runtime.report().functions['async.selected'].unwinds, 1);
    assert.equal(await module.exports.overridden(new Promise(() => {})), 7);
    assert.equal(runtime.report().function_calls, 3);
  } finally { await runtime.close(); delete globalThis[RUNTIME]; await r.close(); }
});

test('function-body declarations preserve hoisting, var redeclarations and lexical closures', () => {
  const source = `function selected() { 'use strict'; var inner; let value = 7; return inner(); function inner() { return value; } }
    function mutable() { function inner() { return inner; } const original=inner; inner=9; return original(); }
    async function asynchronous() { var inner; let value=11; return inner(); function inner() { return value; } }
    module.exports={selected,mutable,asynchronous};`;
  const runtime = { register: () => true }; const p = plan('http://127.0.0.1:1/v1/metrics');
  const output = transform(source, '/tmp/declarations.cjs', 'declarations.cjs', p, runtime);
  const module = { exports: {} }; new Function('require', 'module', output.code)(require, module);
  assert.equal(module.exports.selected(), 7); assert.equal(module.exports.mutable(), 9);
  return module.exports.asynchronous().then(value => assert.equal(value, 11));
});

test('distinct object methods, accessors and same-line callbacks have distinct identities', async () => {
  const source = 'const a={run(){return 1},get value(){return 1},set value(v){}}; const b={run(){return 2}}; const callbacks=[()=>1,()=>2];';
  const p = plan('http://127.0.0.1:1/v1/metrics'); const runtime = { register: () => true };
  const plain = transform(source, '/tmp/identities.cjs', 'identities.cjs', p, runtime);
  assert.equal(new Set(plain.functions.map(f => f.name)).size, 6);
  assert.ok(plain.functions.some(f => f.name === 'identities.a.run'));
  assert.ok(plain.functions.some(f => f.name === 'identities.b.run'));
  const { transpile } = await import('../typescript.mjs');
  const prepared = transpile(source, '/tmp/identities.cts', 'identities.cts', p, '/tmp');
  const typed = transform(prepared.code, '/tmp/identities.cts', 'identities.cts', p, runtime, prepared);
  assert.equal(new Set(typed.functions.map(f => f.name)).size, 6);
});

test('selection uses authoritative UTF-8 byte globs and exclusion precedence', () => {
  const selection = new Selection({ include: ['(?-u).*'], exclude: ['(?-u)blocked'] });
  assert.equal(selection.accepts('blocked', true), false);
  assert.equal(selection.accepts('hello\n'), true);
  assert.equal(new Selection({ include: ['(?-u).'], exclude: [] }).accepts('é'), false);
  assert.equal(new Selection({ include: ['(?-u)\\xc3\\xa9'], exclude: [] }).accepts('é'), true);
  assert.equal(sourceName('/tmp/project/node_modules/a.js', '/tmp/project'), null);
  assert.equal(sourceName('/tmp/other/app.js', '/tmp/project'), null);
});

test('in-memory bodies preserve signatures, recursion, throws, generators, constructors and shadows', async () => {
  const r = await receiver(); const runtime = new Runtime(plan(r.endpoint)); globalThis[RUNTIME] = runtime;
  try {
    const source = `function f(globalThis, Symbol, process) { 'use strict'; if (globalThis < 0) throw Symbol; return globalThis + process; }
      function recursive(n) { return n ? recursive(n-1)+1 : 0; }
      function* generator() { yield 1; yield 2; }
      async function a() { await Promise.resolve(); throw 'async-error'; }
      class Base { constructor(n) { this.n=n; } }
      class Derived extends Base { constructor(n) { super(n); } get value() { return this.n; } }
      const arrow = (value) => value+1;
      module.exports={f, recursive, generator, a, Derived, arrow};`;
    const output = transform(source, '/tmp/application.cjs', 'application.cjs', runtime.plan, runtime);
    const module = { exports: {} }; new Function('require', 'module', output.code)(require, module);
    const api = module.exports;
    assert.equal(api.f.length, 3); assert.equal(api.f(2, 8, 5), 7);
    assert.throws(() => api.f(-1, new Error('same'), 5), /same/);
    assert.equal(api.recursive(3), 3); assert.equal(api.arrow(2), 3);
    const iterator = api.generator(); assert.equal(iterator.next().value, 1); iterator.return();
    await assert.rejects(api.a(), error => error === 'async-error');
    assert.equal(new api.Derived(7).value, 7);
    assert.equal(runtime.report().function_calls, 12);
    assert.equal(runtime.report().functions['application.f'].unwinds, 1);
    assert.equal(runtime.report().functions['application.a'].unwinds, 1);
    assert.ok(output.code.includes('sourceMappingURL'));
    assert.throws(() => transform(MARKER, '/tmp/a.js', 'a.js', runtime.plan, runtime), /already instrumented/);
    assert.throws(() => transform('function f(){eval("1")}', '/tmp/a.js', 'a.js', runtime.plan, runtime), /direct eval/);
    assert.throws(() => transform('const require=1; function f(){}', '/tmp/a.cjs', 'a.cjs', runtime.plan, runtime), /require shadowing/);
  } finally { await runtime.close(); delete globalThis[RUNTIME]; await r.close(); }
});

test('annotations opt in, exclusion wins, and unknown annotations fail clearly', () => {
  const p = plan('http://127.0.0.1:1/v1/metrics'); p.function_matchers.include = ['(?-u)never'];
  const source = '// otelc.instrument\nfunction selected() {}\n// otelc.instrument\nfunction excluded() {}\nfunction plain() {}';
  const output = transform(source, '/tmp/a.js', 'a.js', p, { register: () => true });
  assert.deepEqual(output.functions.map(value => value.selected), [true, false, false]);
  assert.throws(() => transform('// otelc.unknown\nfunction f(){}', '/tmp/a.js', 'a.js', p, { register: () => true }), /unsupported otelc annotation/);
  p.annotations.read_existing = false;
  assert.equal(transform(source, '/tmp/a.js', 'a.js', p, { register: () => true }).functions.some(value => value.selected), false);
});

test('real ESM and CommonJS hooks keep original files intact and export bounded SDK metrics', async () => {
  const r = await receiver(); const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-node-test-'));
  const esm = path.join(directory, 'app.mjs'), cjs = path.join(directory, 'helper.cjs');
  fs.writeFileSync(cjs, 'module.exports = function helper(n){return n*2;}');
  const source = "import helper from './helper.cjs'; export function run(n){return helper(n)+1;}";
  fs.writeFileSync(esm, source);
  const application = await install(plan(r.endpoint), directory);
  try {
    await assert.rejects(install(plan(r.endpoint), directory), /already installed/);
    const module = await import(pathToFileURL(esm).href);
    assert.equal(module.run(5), 11); assert.equal(application.runtime.report().function_calls, 2);
    assert.equal(fs.readFileSync(esm, 'utf8'), source);
    const collected = await application.runtime.reader.collect();
    const metrics = collected.resourceMetrics.scopeMetrics.flatMap(scope => scope.metrics);
    assert.equal(metrics.find(metric => metric.descriptor.name === 'otelc.function.calls').dataPoints.reduce((sum, point) => sum + point.value, 0), 2);
    assert.equal(metrics.find(metric => metric.descriptor.name === 'otelc.function.duration').dataPoints[0].value.count, 1);
    await application.close();
    assert.equal(application.runtime.report().export_finished, true);
    assert.ok(r.requests.some(request => request.body.length > 0 && request.headers['content-type'] === 'application/x-protobuf' && request.url === '/v1/metrics'));
    assert.equal(inspect(esm, plan(r.endpoint), directory).functions[0].name, 'app.run');
    const skipped = plan(r.endpoint); skipped.source_matchers.include = ['(?-u)never'];
    assert.deepEqual(inspect(esm, skipped, directory).functions, []);
  } finally { await application.close(); await r.close(); fs.rmSync(directory, { recursive: true }); }
  await assert.rejects(install({ ...plan(r.endpoint), execution_available: false }), /executable/);
});

test('live control keeps admitted calls, rejects malformed requests, and enforces capacity', async () => {
  const r = await receiver(); const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-node-control-'));
  fs.chmodSync(directory, 0o700);
  const p = plan(r.endpoint); p.runtime.control_socket = path.join(directory, 'metrics.sock'); p.runtime.max_functions = 1; p.runtime.max_active_calls = 1;
  const runtime = new Runtime(p);
  try {
    await runtime.bindControl(); assert.equal(fs.statSync(p.runtime.control_socket).mode & 0o777, 0o600);
    assert.equal((await control(p.runtime.control_socket, 'status\n')).pid, process.pid);
    const token = runtime.enter('a'); assert.equal(runtime.enter('a'), 0); assert.equal(runtime.enter('b'), 0);
    await control(p.runtime.control_socket, 'disable\n'); assert.equal(runtime.enter('a'), 0); runtime.exit(token, true);
    assert.equal(runtime.report().function_calls, 1);
    assert.equal((await control(p.runtime.control_socket, 'enable\n')).metrics_enabled, true);
    assert.ok((await control(p.runtime.control_socket, 'wrong\n')).error);
    assert.ok((await control(p.runtime.control_socket, 'x'.repeat(20))).error);
    assert.ok((await control(p.runtime.control_socket, 'incomplete')).error);
    runtime.enter('a'); assert.equal(runtime.report().losses.incomplete, 1);
    const report = await runtime.close(); assert.equal(report.losses.incomplete, 1); assert.equal(report.losses.active_call_capacity, 1); assert.equal(report.losses.function_capacity, 1);
    assert.equal(fs.existsSync(p.runtime.control_socket), false);
    const occupied = new Runtime(p); fs.writeFileSync(p.runtime.control_socket, 'occupied');
    await assert.rejects(occupied.bindControl()); await occupied.close(); assert.equal(fs.readFileSync(p.runtime.control_socket, 'utf8'), 'occupied');
    fs.chmodSync(directory, 0o755); const insecure = new Runtime(p); await assert.rejects(insecure.bindControl(), /0700/); await insecure.close();
  } finally { await runtime.close(); await r.close(); fs.rmSync(directory, { recursive: true }); }
});

test('OTLP headers, partial success, invalid responses and failed transport are accounted for', async () => {
  assert.deepEqual(headers({ OTEL_EXPORTER_OTLP_HEADERS: 'x-a=hello%20world' }), { 'x-a': 'hello world' });
  assert.deepEqual(headers({ OTEL_EXPORTER_OTLP_HEADERS: 'x-a=old', OTEL_EXPORTER_OTLP_METRICS_HEADERS: 'x-b=new' }), { 'x-b': 'new' });
  assert.throws(() => headers({ OTEL_EXPORTER_OTLP_HEADERS: 'invalid' }), /invalid/);
  assert.throws(() => headers({ OTEL_EXPORTER_OTLP_HEADERS: 'x=%0d' }), /invalid/);
  for (const handler of [response => { response.statusCode = 503; response.end(); }, response => response.end(Buffer.from([10, 2, 8, 1])), response => response.end(Buffer.from([255])), response => response.end(Buffer.alloc(65537)), () => {}]) {
    const r = await receiver(handler); const runtime = new Runtime(plan(r.endpoint));
    const token = runtime.enter('selected'); runtime.exit(token, false);
    await runtime.close(); assert.ok(runtime.exportLoss >= 1);
    await r.close();
  }
  const runtime = new Runtime(plan('http://127.0.0.1:1/v1/metrics')); runtime.enter('a'); await runtime.close(); assert.ok(runtime.exportLoss >= 1);
});

test('external Node entry point runs untouched and annotated apps; doctor and inspect work', async () => {
  const cli = path.join(root, 'target/debug/quux-otelc');
  assert.ok(fs.existsSync(cli), 'build the CLI before node-check');
  const r = await receiver(); const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-node-launch-'));
  const config = path.join(directory, 'policy.toml');
  fs.writeFileSync(config, fs.readFileSync(path.join(root, 'examples/javascript.toml'), 'utf8').replace('http://127.0.0.1:4318', r.endpoint.replace('/v1/metrics', '')));
  try {
    for (const [filename, expected, calls] of [['javascript_app.mjs', '70', 17], ['javascript_annotated.cjs', '30', 2]]) {
      const source = path.join(root, 'examples/apps', filename), original = fs.readFileSync(source);
      const report = path.join(directory, 'report.json');
      const result = await child(cli, ['--config', config, 'node', source], { OTELC_REPORT_PATH: report });
      assert.equal(result.status, 0, result.stderr); assert.equal(result.stdout.trim(), expected);
      const metrics = JSON.parse(fs.readFileSync(report)); assert.equal(metrics.function_calls, calls); assert.equal(metrics.export_loss, 0); assert.equal(metrics.export_finished, true);
      assert.deepEqual(fs.readFileSync(source), original);
    }
    const doctor = await child(cli, ['--config', config, '--language', 'javascript', 'doctor']); assert.equal(doctor.status, 0, doctor.stderr); assert.match(doctor.stdout, /OTLP/);
    const inventory = await child(cli, ['--config', config, '--language', 'javascript', 'inspect', 'examples/apps/javascript_annotated.cjs', '--json']); assert.equal(inventory.status, 0, inventory.stderr); assert.equal(JSON.parse(inventory.stdout).functions.filter(value => value.selected).length, 2);
  } finally { await r.close(); fs.rmSync(directory, { recursive: true }); }
});
