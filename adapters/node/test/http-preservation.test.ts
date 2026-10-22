import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { propagation, ROOT_CONTEXT, trace, createTraceState } from '@opentelemetry/api';
import { CompositePropagator, W3CBaggagePropagator, W3CTraceContextPropagator } from '@opentelemetry/core';
import { errorType, injectHeaders } from '../src/http-preservation.ts';

assert.equal(propagation.setGlobalPropagator(new CompositePropagator({
  propagators: [new W3CTraceContextPropagator(), new W3CBaggagePropagator()],
})), true);
after(() => propagation.disable());
const active = propagation.setBaggage(trace.setSpanContext(ROOT_CONTEXT, {
  traceId: 'a'.repeat(32), spanId: 'b'.repeat(16), traceFlags: 1, traceState: createTraceState('vendor=opaque'),
}), propagation.createBaggage({ session: { value: 'ordinary' } }));

test('raw pairs retain duplicates, spelling and order while W3C fields are replaced on a private copy', () => {
  const headers = Object.freeze(['X-Business', 'one', 'X-Business', 'two',
    'TraceParent', 'old', 'traceparent', 'also-old', 'TraceState', 'old-state', 'Baggage', 'old-baggage']);
  const before = [...headers];
  const injected = injectHeaders(active, headers);
  assert.equal(Array.isArray(injected), true);
  assert.deepEqual(injected, ['X-Business', 'one', 'X-Business', 'two',
    'traceparent', '00-' + 'a'.repeat(32) + '-' + 'b'.repeat(16) + '-01',
    'tracestate', 'vendor=opaque', 'baggage', 'session=ordinary']);
  assert.deepEqual(headers, before); assert.notEqual(injected, headers);
});

test('object carriers preserve frozen caller data and literal prototype-named headers', () => {
  const values = ['one', 'two']; Object.freeze(values);
  const headers = { 'X-Business': values };
  Object.defineProperty(headers, '__proto__', { value: 'literal-header', enumerable: true });
  Object.freeze(headers);
  const injected = injectHeaders(active, headers) as Record<string, unknown>;
  assert.equal(Object.getPrototypeOf(injected), null);
  assert.equal(injected.__proto__, 'literal-header');
  assert.equal(injected['X-Business'], values);
  assert.equal('traceparent' in headers, false);
  assert.equal(injected.traceparent, '00-' + 'a'.repeat(32) + '-' + 'b'.repeat(16) + '-01');
});

test('missing headers receive a new carrier and invalid raw pairs retain Node validation shape', () => {
  const injected = injectHeaders(active, undefined) as Record<string, unknown>;
  assert.equal(typeof injected.traceparent, 'string');
  const invalid = Object.freeze(['X-Business', 'one', 'dangling']);
  assert.deepEqual(injectHeaders(active, invalid), invalid);
  assert.notEqual(injectHeaders(active, invalid), invalid);
});

test('no-context injection retains a raw carrier and error metadata never reads application formatters', () => {
  const headers = Object.freeze(['X-Business', 'one']);
  assert.deepEqual(injectHeaders(ROOT_CONTEXT, headers), headers);
  let reads = 0;
  const error = new TypeError();
  for (const key of ['name', 'message', 'stack']) Object.defineProperty(error, key,
    { get() { reads++; throw 'forbidden'; } });
  assert.equal(errorType(error), 'TypeError');
  assert.equal(reads, 0);
});
