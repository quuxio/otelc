import type { Context } from '@opentelemetry/api';
import { AsyncLocalStorageContextManager } from '@opentelemetry/context-async-hooks';

export type ContextTokenType = ReturnType<AsyncLocalStorageContextManager['attach']>;

/** Use the official context manager; task-off keeps probe scopes synchronous only. */
export class PolicyContextManager extends AsyncLocalStorageContextManager {
  readonly #tasks: boolean;
  #scope: Context | undefined;
  #generation = 0;

  constructor(tasks: boolean) { super(); this.#tasks = tasks; }

  override active(): Context { return this.#scope ?? super.active(); }

  override attach(value: Context): ContextTokenType {
    let delegate = this.#tasks ? super.attach(value) : undefined;
    let previous = this.#scope;
    if (!this.#tasks) this.#scope = value;
    const generation = this.#generation;
    let disposed = false;
    return { dispose: () => {
      if (disposed) return;
      disposed = true;
      const scope = previous;
      const token = delegate;
      previous = undefined;
      delegate = undefined;
      if (generation !== this.#generation) return;
      if (token) token.dispose();
      else this.#scope = scope;
    } };
  }

  override with<A extends unknown[], F extends (...args: A) => ReturnType<F>>(
    value: Context, callback: F, thisArg?: ThisParameterType<F>, ...args: A
  ): ReturnType<F> {
    if (this.#tasks) return super.with(value, callback, thisArg, ...args);
    // An explicit SDK/library scope takes precedence over the caller's probe scope.
    const previous = this.#scope;
    this.#scope = undefined;
    try { return super.with(value, callback, thisArg, ...args); }
    finally { this.#scope = previous; }
  }

  override disable(): this { this.#generation++; this.#scope = undefined; return super.disable(); }
}
