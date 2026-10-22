/** Independent live switches for the SDK pipeline owned by otelc. */
export class MetricControls {
  #collection: boolean;
  #emission: boolean;
  #generation = 0;

  constructor(collection: boolean, emission: boolean) {
    this.#collection = collection;
    this.#emission = emission;
  }

  get collection(): boolean { return this.#collection; }
  get emission(): boolean { return this.#emission; }
  get generation(): number { return this.#generation; }

  /** Invalidate observations from callbacks that cross a collection transition. */
  setCollection(enabled: boolean): void {
    if (enabled !== this.#collection) this.#generation++;
    this.#collection = enabled;
  }

  /** Suppress future exports; already admitted requests must settle separately. */
  setEmission(enabled: boolean): void { this.#emission = enabled; }

  /** Admit only observations made in the same enabled collection generation. */
  accepts(generation: number): boolean {
    return this.#collection && generation === this.#generation;
  }
}
