import { propagation } from '@opentelemetry/api';
import type { Context, TextMapSetter } from '@opentelemetry/api';
import { nativeObserver } from './compiler/promise-observer.mjs';

const nativeType = nativeObserver().typeName;
if (typeof nativeType !== 'function') throw new Error('rebuild the exact-version Node observer for HTTP error metadata');
const arrayIsArray = Array.isArray;
const define = Object.defineProperty;
const assign = Object.assign;
const create = Object.create;
const string = String;
const lower = Function.call.bind(String.prototype.toLowerCase) as (value: string) => string;
const inject = propagation.inject.bind(propagation);

/** Observe V8 type metadata without inspecting error accessors, formatters or proxy traps. */
export function errorType(error: unknown): string { return nativeType(error); }

function put(array: unknown[], index: number, value: unknown): void {
  define(array, string(index), { value, enumerable: true, configurable: true, writable: true });
}

const rawSetter: TextMapSetter<unknown[]> = { set(carrier, key, value) {
  let next = 0;
  for (let index = 0; index < carrier.length; index += 2) {
    const name = carrier[index];
    if (typeof name === 'string' && lower(name) === lower(key)) continue;
    put(carrier, next++, name); put(carrier, next++, carrier[index + 1]);
  }
  carrier.length = next;
  put(carrier, next++, key); put(carrier, next, value);
} };

/** Preserve Node's alternating raw header pairs; only the private copy receives propagation. */
export function injectHeaders(active: Context, headers: unknown): object {
  if (arrayIsArray(headers)) {
    const copy: unknown[] = [];
    for (let index = 0; index < headers.length; index++) put(copy, index, headers[index]);
    // Keep malformed input malformed so Node itself reports the original error.
    if (copy.length % 2) return copy;
    inject(active, copy, rawSetter);
    return copy;
  }
  const copy = assign(create(null) as Record<string, unknown>, headers);
  inject(active, copy);
  return copy;
}
