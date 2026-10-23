import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { setImmediate as immediate } from 'node:timers/promises';
import { describe, it } from 'node:test';
import { createContextKey, ROOT_CONTEXT } from '@opentelemetry/api';
import { PolicyContextManager } from '../src/policy-context-manager.ts';

const KEY = createContextKey('otelc.context.fixture');
const PARENT = ROOT_CONTEXT.setValue(KEY, 'parent');
const CHILD = ROOT_CONTEXT.setValue(KEY, 'child');

describe('common task policy and official context scopes', () => {
  for (const tasks of [false, true]) {
    it(`does not resurrect a disabled context when a late probe finally disposes: tasks=${tasks}`, () => {
      const manager = new PolicyContextManager(tasks).enable();
      const outer = manager.attach(PARENT);
      const inner = manager.attach(CHILD);
      manager.disable();
      inner.dispose();
      assert.equal(manager.active(), ROOT_CONTEXT);
      outer.dispose();
      assert.equal(manager.active(), ROOT_CONTEXT);
    });

    it(`keeps nested scopes and explicit SDK scopes intact with tasks=${tasks}`, () => {
      const manager = new PolicyContextManager(tasks).enable();
      try {
        const outer = manager.attach(PARENT);
        assert.equal(manager.active().getValue(KEY), 'parent');
        const inner = manager.attach(CHILD);
        assert.equal(manager.active().getValue(KEY), 'child');
        inner.dispose(); inner.dispose();
        assert.equal(manager.active().getValue(KEY), 'parent');
        const receiver = { expected: 3 };
        const result = manager.with(CHILD, function (this: typeof receiver, value: number) {
          assert.equal(this, receiver);
          assert.equal(manager.active().getValue(KEY), 'child');
          return value + this.expected;
        }, receiver, 4);
        assert.equal(result, 7);
        assert.equal(manager.active().getValue(KEY), 'parent');
        const failure = new Error('original');
        assert.throws(() => manager.with(CHILD, () => { throw failure; }), value => value === failure);
        assert.equal(manager.active().getValue(KEY), 'parent');
        outer.dispose(); outer.dispose();
        assert.equal(manager.active(), ROOT_CONTEXT);
      } finally { manager.disable(); }
    });

    it(`propagates probe context to tasks only when selected: tasks=${tasks}`, async () => {
      const manager = new PolicyContextManager(tasks).enable();
      try {
        const token = manager.attach(PARENT);
        const observed = immediate().then(() => manager.active().getValue(KEY));
        token.dispose();
        assert.equal(manager.active(), ROOT_CONTEXT, 'probe must restore the caller before returning');
        assert.equal(await observed, tasks ? 'parent' : undefined);
        await immediate();
        assert.equal(manager.active(), ROOT_CONTEXT, 'a later unrelated task must not inherit the probe');
      } finally { manager.disable(); }
    });

    it(`retains official binding identity, listener removal and explicit asynchronous scope: tasks=${tasks}`, async () => {
      const manager = new PolicyContextManager(tasks).enable();
      try {
        const emitter = new EventEmitter();
        manager.bind(PARENT, emitter);
        let calls = 0;
        const listener = () => { calls++; assert.equal(manager.active().getValue(KEY), 'parent'); };
        emitter.on('work', listener);
        manager.with(CHILD, () => emitter.emit('work'));
        emitter.removeListener('work', listener);
        emitter.emit('work');
        assert.equal(calls, 1);
        const token = manager.attach(PARENT);
        const result = manager.with(CHILD, async () => { await immediate(); return manager.active().getValue(KEY); });
        token.dispose();
        assert.equal(await result, 'child', 'task-off must not erase an explicit library/application context');
        manager.attach(PARENT);
        manager.disable();
        assert.equal(manager.active(), ROOT_CONTEXT);
      } finally { manager.disable(); }
    });
  }
});
