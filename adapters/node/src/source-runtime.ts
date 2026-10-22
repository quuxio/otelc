import { context, SpanStatusCode, trace, ValueType } from '@opentelemetry/api';
import type { Counter, Histogram, Span, SpanContext } from '@opentelemetry/api';
import { PolicyContextManager } from './policy-context-manager.ts';
import type { ContextTokenType } from './policy-context-manager.ts';
import { SourceSdk } from './source-sdk.ts';
import type { MetricControls } from './metric-controls.ts';
import { epochNs, monotonicNs, sdkTime } from './source-clock.ts';
import { SourceLifetimes } from './source-lifetimes.ts';
import { PromiseObserver } from './compiler/promise-observer.mjs';
import type { IAsyncSite, ISourcePlan } from './source-plan.ts';

interface IFunction { count: number; unwinds: number; attributes: { function: string; language: string } }
interface IFrame { function: IFunction; started: bigint; epoch: bigint; metrics: boolean; span?: Span }
interface IScope { token: ContextTokenType }

export const RUNTIME = Symbol.for('quux.otelc.runtime');

/** Source probes own admission/scopes; all metric aggregation and span production use the SDK. */
export class SourceRuntime {
  readonly plan: ISourcePlan;
  readonly sdk: SourceSdk;
  readonly controls: MetricControls;
  readonly losses = { function_capacity: 0, active_call_capacity: 0, invalid: 0, async_origin: 0, incomplete: 0 };
  readonly #manager: PolicyContextManager;
  readonly #functions = new Map<string, IFunction>();
  readonly #frames = new Map<number, IFrame>();
  readonly #calls: Counter;
  readonly #unwinds: Counter;
  readonly #duration: Histogram;
  readonly lifetimes?: SourceLifetimes;
  #observer?: PromiseObserver;
  #ownsContext = false;
  #closed = false;
  #next = 1;

  private constructor(plan: ISourcePlan, sdk: SourceSdk) {
    this.plan = plan; this.sdk = sdk; this.controls = sdk.controls;
    this.#manager = new PolicyContextManager(plan.context.tasks).enable();
    const meter = sdk.metrics('io.quux.otelc.functions');
    this.#calls = meter.counter('otelc.function.calls', { unit: '{call}', valueType: ValueType.INT });
    this.#unwinds = meter.counter('otelc.function.unwinds', { unit: '{observation}', valueType: ValueType.INT });
    this.#duration = meter.histogram('otelc.function.duration', { unit: 's' });
    if (plan.lifetimes.enabled) this.lifetimes = new SourceLifetimes(plan, sdk.metrics('io.quux.otelc.lifetimes'),
      sdk.tracerProvider.getTracer('io.quux.otelc.lifetimes', '0.2.0'), () => this.observing, () => this.controls.collection);
    sdk.onCollectionChange = () => this.lifetimes?.gauges();
  }

  /** Refuse an existing owner and release our own SDKs without disabling that owner. */
  static async start(plan: ISourcePlan): Promise<SourceRuntime> {
    const runtime = new SourceRuntime(plan, await SourceSdk.start(plan));
    if (context.setGlobalContextManager(runtime.#manager)) { runtime.#ownsContext = true; return runtime; }
    await runtime.shutdown();
    throw new Error('an OpenTelemetry context manager is already installed');
  }

  get closed(): boolean { return this.#closed; }
  get observing(): boolean { return !this.#closed && (this.controls.collection || this.plan.traces.enabled); }
  lose(reason: keyof SourceRuntime['losses']): void { this.losses[reason]++; }
  rejectTrace(_reason: string): number { return 0; }

  register(name: string): boolean {
    if (this.#functions.has(name)) return true;
    if (this.#functions.size >= this.plan.runtime.max_functions || Buffer.byteLength(name) > 1024) {
      this.losses.function_capacity++; return false;
    }
    this.#functions.set(name, { count: 0, unwinds: 0, attributes: { function: name, language: this.plan.language } });
    return true;
  }

  enter(name: string): number {
    if (!this.observing) return 0;
    this.#observer?.flush();
    if (!this.register(name)) return 0;
    if (this.#frames.size >= this.plan.runtime.max_active_calls || this.#next >= Number.MAX_SAFE_INTEGER) {
      this.losses.active_call_capacity++; return 0;
    }
    const token = this.#next++;
    const started = monotonicNs();
    const epoch = epochNs(started);
    const span = this.plan.traces.enabled ? this.sdk.tracerProvider.getTracer('io.quux.otelc.functions', '0.2.0')
      .startSpan(name, { startTime: sdkTime(epoch), attributes: { 'code.function.name': name, 'otelc.language': this.plan.language } }) : undefined;
    this.#frames.set(token, { function: this.#functions.get(name)!, started, epoch,
      metrics: this.controls.collection, span });
    return token;
  }

  exit(token: number, unwound: boolean, ended?: bigint | null): void {
    const frame = this.#frames.get(token);
    if (!frame) return;
    this.#frames.delete(token);
    const elapsed = (ended ?? monotonicNs()) - frame.started;
    frame.function.count++; frame.function.unwinds += Number(unwound);
    if (frame.metrics) {
      this.#calls.add(1, frame.function.attributes);
      this.#duration.record(Number(elapsed) / 1e9, frame.function.attributes);
      if (unwound) this.#unwinds.add(1, frame.function.attributes);
    }
    if (unwound) frame.span?.setStatus({ code: SpanStatusCode.ERROR });
    frame.span?.end(sdkTime(frame.epoch + elapsed));
  }

  attach(token: number, previous?: IScope | null): IScope | null {
    const span = this.#frames.get(token)?.span;
    if (!span) return null;
    if (previous && trace.getSpan(context.active()) === span) return previous;
    return { token: this.#manager.attach(trace.setSpan(context.active(), span)) };
  }

  detach(scope: IScope | null): void { scope?.token.dispose(); }
  suspend<T>(scope: IScope | null, value: T): T { this.detach(scope); return value; }
  parent(): SpanContext | undefined {
    const value = trace.getSpanContext(context.active());
    // Lifetime metadata keeps IDs, never an application context or Span object.
    return value ? { traceId: value.traceId, spanId: value.spanId, traceFlags: value.traceFlags, isRemote: value.isRemote } : undefined;
  }

  registerAsyncSites(sites: IAsyncSite[]): void {
    if (!sites.length) return;
    this.#observer ??= new PromiseObserver(this);
    this.#observer.register(sites);
  }
  enterAsync(name: string, id: number): number { return this.#observer?.enter(name, id) ?? 0; }

  snapshot() {
    return { pid: process.pid, language: this.plan.language, launch_id: this.plan.launch_id,
      function_completions: [...this.#functions.values()].reduce((total, value) => total + value.count, 0),
      active_functions: this.#frames.size, runtime_losses: { ...this.losses },
      native_callback_failures: this.#observer?.failures() ?? 0,
      lifetimes: this.lifetimes?.status(), lifetime_lost: this.lifetimes?.status().lost ?? 0,
      handoff: this.sdk.handoffStatus(), span_observation: this.sdk.spanObservation() };
  }

  async status(): Promise<object> { return { ...await this.sdk.status(), ...this.snapshot() }; }

  async flush(): Promise<void> { this.#observer?.flush(); this.lifetimes?.gauges(); await this.sdk.flush(); }

  /** Synchronous final evidence only; no work is scheduled on the application's event loop. */
  checkpoint(): void {
    this.#observer?.flush(); this.lifetimes?.gauges();
    this.sdk.checkpoint(this.snapshot());
  }

  shutdown(): Promise<void> {
    if (!this.#closed) {
      try { this.#observer?.close(); }
      catch { this.losses.invalid++; }
      this.lifetimes?.close(); this.#closed = true;
      this.losses.incomplete += this.#frames.size; this.#frames.clear();
      if (this.#ownsContext) context.disable();
      this.#manager.disable();
    }
    this.checkpoint();
    return this.sdk.shutdown();
  }
}
