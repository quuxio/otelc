// otelc.instrument
class AnnotatedItem {
  constructor(public readonly value: number) {}
  compute() { return this.value * 3; }
}
function make(value: number) { return new AnnotatedItem(value); }
export const items = [make(7), make(9)];
console.log(items.reduce((sum, item) => sum + item.compute(), 0));
