import assert from 'node:assert/strict';
import { once } from 'node:events';
import { setImmediate } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { Worker } from 'node:worker_threads';
import test from 'node:test';
import { nativeObserver } from '../src/compiler/promise-observer.mjs';

const native = nativeObserver();
const filename = fileURLToPath(new URL('../../../build/tools/otelc_node_observer-' + process.versions.node + '.node', import.meta.url));

test('reads actual pending/fulfilled/rejected states without inspecting thenables', () => {
  assert.equal(native.state(new Promise(() => { /* Deliberately unresolved application Promise. */ })), 0);
  assert.equal(native.state(Promise.resolve(42)), 1);
  const rejected = Promise.reject('owned rejection');
  void rejected.catch(() => { /* This test application explicitly owns this rejection. */ });
  assert.equal(native.state(rejected), 2);
  let getters = 0;
  const thenable = { get then() { getters++; return (): void => {}; } };
  assert.throws(() => Reflect.apply(native.state, undefined, [thenable]), /expected a Promise/);
  assert.throws(() => Reflect.apply(native.state, undefined, []), /expected a Promise/);
  assert.equal(getters, 0);
});

test('frame capture bypasses application formatters and inherited array setters', () => {
  let formatters = 0, setters = 0;
  const original = Error.prepareStackTrace;
  const property = Object.getOwnPropertyDescriptor(Array.prototype, '0');
  let frames: ReturnType<typeof native.frames>;
  try {
    Error.prepareStackTrace = () => { formatters++; return 'application formatter'; };
    Object.defineProperty(Array.prototype, '0', { configurable: true, set() { setters++; } });
    frames = native.frames();
  } finally {
    Error.prepareStackTrace = original;
    if (property) Object.defineProperty(Array.prototype, '0', property);
    else Reflect.deleteProperty(Array.prototype, '0');
  }
  assert.equal(formatters, 0); assert.equal(setters, 0);
  assert.ok(frames.length > 0 && frames.length <= 128);
  assert.ok(frames.some(frame => frame[0] === import.meta.url));
  assert.ok(frames.every(frame => frame.length === 4 && Number.isInteger(frame[1])));
});

test('an application sourceURL comment cannot impersonate the actual script identity', () => {
  const frames = vm.runInNewContext('native.frames()\n//# sourceURL=spoofed.cjs', { native },
    { filename: '/original/application.cjs' }) as ReturnType<typeof native.frames>;
  assert.equal(frames[0][0], '/original/application.cjs');
});

test('checkpoint observes completed jobs and its stop token is idempotent', async t => {
  let calls = 0;
  const generation = native.checkpoint(() => { calls++; });
  t.after(() => native.stopCheckpoint(generation));
  await setImmediate();
  assert.ok(calls > 0);
  assert.equal(native.stopCheckpoint(generation), true);
  assert.equal(native.stopCheckpoint(generation), false);
  const stopped = calls;
  await setImmediate();
  assert.equal(calls, stopped);
  assert.equal(native.checkpointStats().active, false);
});

test('repeated replacement is bounded and obsolete stop tokens cannot stop the owner', async t => {
  let oldCalls = 0, currentCalls = 0;
  const first = native.checkpoint(() => { oldCalls++; });
  let current = first;
  for (let i = 0; i < 10000; i++) current = native.checkpoint(() => { currentCalls++; });
  t.after(() => native.stopCheckpoint(current));
  assert.equal(native.stopCheckpoint(first), false);
  assert.equal(Reflect.apply(native.stopCheckpoint, undefined, []), false);
  assert.equal(Reflect.apply(native.stopCheckpoint, undefined, ['wrong']), false);
  await setImmediate();
  assert.equal(oldCalls, 0); assert.ok(currentCalls > 0);
  assert.equal(native.checkpointStats().generation, current);
});

test('callback failure is counted and stopped without inspecting the thrown object', async () => {
  let getters = 0;
  const thrown = { get message() { getters++; return 'forbidden'; }, get stack() { getters++; return 'forbidden'; } };
  const before = native.checkpointStats().failures;
  native.checkpoint(() => { throw thrown; });
  await setImmediate();
  assert.equal(native.checkpointStats().failures, before + 1);
  assert.equal(native.checkpointStats().active, false);
  assert.equal(getters, 0);
  await setImmediate();
  assert.equal(native.checkpointStats().failures, before + 1);
  assert.throws(() => Reflect.apply(native.checkpoint, undefined, []), /expected own callback/);
});

test('a throwing superseded callback cannot stop its replacement', async t => {
  let current = 0, calls = 0;
  const before = native.checkpointStats().failures;
  native.checkpoint(() => {
    current = native.checkpoint(() => { calls++; });
    throw 'old callback failure';
  });
  t.after(() => native.stopCheckpoint(current));
  await setImmediate(); await setImmediate();
  assert.ok(calls > 0);
  assert.equal(native.checkpointStats().active, true);
  assert.equal(native.checkpointStats().failures, before + 1);
});

test('worker environments clean up active callbacks on natural exit and termination', async () => {
  for (let i = 0; i < 16; i++) {
    const terminate = i >= 8;
    const worker = new Worker(`const {parentPort}=require('node:worker_threads');
      const native=require(${JSON.stringify(filename)});
      native.checkpoint(()=>{});
      parentPort.postMessage(native.checkpointStats().active);
      ${terminate ? 'setInterval(()=>{},1000);' : 'parentPort.close();'}`, { eval: true, execArgv: [] });
    const exited = once(worker, 'exit');
    assert.deepEqual(await once(worker, 'message'), [true]);
    if (terminate) await worker.terminate();
    const [code] = await exited;
    assert.equal(code, terminate ? 1 : 0);
  }
});
