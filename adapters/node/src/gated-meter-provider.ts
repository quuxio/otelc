import type {
  Attributes, BatchObservableCallback, Counter, Gauge, Histogram, Meter, MeterOptions,
  MeterProvider, MetricOptions, Observable, ObservableCallback, ObservableCounter,
  ObservableGauge, ObservableUpDownCounter, UpDownCounter,
} from '@opentelemetry/api';
import type { MetricControls } from './metric-controls.ts';

function gateAdd<A extends Attributes>(instrument: Counter<A>, controls: MetricControls): Counter<A> {
  return { add(value, attributes, context) {
    if (controls.collection) instrument.add(value, attributes, context);
  } };
}

function gateRecord<A extends Attributes>(instrument: Histogram<A>, controls: MetricControls): Histogram<A> {
  return { record(value, attributes, context) {
    if (controls.collection) instrument.record(value, attributes, context);
  } };
}

function gateObservable<A extends Attributes>(instrument: Observable<A>, controls: MetricControls): Observable<A> {
  const callbacks = new WeakMap<ObservableCallback<A>, ObservableCallback<A>>();
  return {
    addCallback(callback) {
      let wrapped = callbacks.get(callback);
      if (!wrapped) {
        wrapped = result => {
          if (!controls.collection) return;
          const generation = controls.generation;
          return callback({ observe(value, attributes) {
            if (controls.accepts(generation)) result.observe(value, attributes);
          } });
        };
        callbacks.set(callback, wrapped);
      }
      instrument.addCallback(wrapped);
    },
    removeCallback(callback) {
      const wrapped = callbacks.get(callback);
      if (wrapped) instrument.removeCallback(wrapped);
    },
  };
}

class GatedMeter implements Meter {
  readonly #meter: Meter;
  readonly #controls: MetricControls;
  readonly #observables: WeakMap<object, Observable>;
  readonly #batchCallbacks = new WeakMap<object, BatchObservableCallback>();

  constructor(meter: Meter, controls: MetricControls, observables: WeakMap<object, Observable>) {
    this.#meter = meter;
    this.#controls = controls;
    this.#observables = observables;
  }

  createCounter<A extends Attributes>(name: string, options?: MetricOptions): Counter<A> {
    return gateAdd(this.#meter.createCounter<A>(name, options), this.#controls);
  }

  createUpDownCounter<A extends Attributes>(name: string, options?: MetricOptions): UpDownCounter<A> {
    return gateAdd(this.#meter.createUpDownCounter<A>(name, options), this.#controls);
  }

  createHistogram<A extends Attributes>(name: string, options?: MetricOptions): Histogram<A> {
    return gateRecord(this.#meter.createHistogram<A>(name, options), this.#controls);
  }

  createGauge<A extends Attributes>(name: string, options?: MetricOptions): Gauge<A> {
    return gateRecord(this.#meter.createGauge<A>(name, options), this.#controls);
  }

  #observable<A extends Attributes>(instrument: Observable<A>): Observable<A> {
    const wrapped = gateObservable(instrument, this.#controls);
    this.#observables.set(wrapped, instrument);
    return wrapped;
  }

  createObservableCounter<A extends Attributes>(name: string, options?: MetricOptions): ObservableCounter<A> {
    return this.#observable(this.#meter.createObservableCounter<A>(name, options));
  }

  createObservableGauge<A extends Attributes>(name: string, options?: MetricOptions): ObservableGauge<A> {
    return this.#observable(this.#meter.createObservableGauge<A>(name, options));
  }

  createObservableUpDownCounter<A extends Attributes>(name: string, options?: MetricOptions): ObservableUpDownCounter<A> {
    return this.#observable(this.#meter.createObservableUpDownCounter<A>(name, options));
  }

  #unwrap<A extends Attributes>(instrument: Observable<A>): Observable<A> {
    // The registry pairs a wrapper with its original instrument and attribute type.
    return (this.#observables.get(instrument) ?? instrument) as Observable<A>;
  }

  addBatchObservableCallback<A extends Attributes>(callback: BatchObservableCallback<A>, observables: Observable<A>[]): void {
    let wrapped = this.#batchCallbacks.get(callback) as BatchObservableCallback<A> | undefined;
    if (!wrapped) {
      wrapped = result => {
        if (!this.#controls.collection) return;
        const generation = this.#controls.generation;
        return callback({ observe: (instrument, value, attributes) => {
          if (this.#controls.accepts(generation)) result.observe(this.#unwrap(instrument), value, attributes);
        } });
      };
      this.#batchCallbacks.set(callback, wrapped);
    }
    // The SDK retains its own duplicate/set-membership and invalid-instrument rules.
    this.#meter.addBatchObservableCallback(wrapped, observables.map(instrument => this.#unwrap(instrument)));
  }

  removeBatchObservableCallback<A extends Attributes>(callback: BatchObservableCallback<A>, observables: Observable<A>[]): void {
    const wrapped = this.#batchCallbacks.get(callback) as BatchObservableCallback<A> | undefined;
    if (wrapped) this.#meter.removeBatchObservableCallback(wrapped, observables.map(instrument => this.#unwrap(instrument)));
  }
}

/** Gate public metric APIs, including instruments cached before live enable/disable. */
export class GatedMeterProvider implements MeterProvider {
  readonly #provider: MeterProvider;
  readonly #controls: MetricControls;
  readonly #meters = new WeakMap<Meter, Meter>();
  readonly #observables = new WeakMap<object, Observable>();

  constructor(provider: MeterProvider, controls: MetricControls) {
    this.#provider = provider;
    this.#controls = controls;
  }

  getMeter(name: string, version?: string, options?: MeterOptions): Meter {
    const meter = this.#provider.getMeter(name, version, options);
    let wrapped = this.#meters.get(meter);
    if (!wrapped) {
      wrapped = new GatedMeter(meter, this.#controls, this.#observables);
      this.#meters.set(meter, wrapped);
    }
    return wrapped;
  }
}
