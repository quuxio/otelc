class WorkItem<T extends number> {
  constructor(public readonly value: T) {}
  compute() { return this.value * 3; }
}
function make(value: number) { return new WorkItem(value); }
export const items = [make(7), make(9)];
console.log(items.reduce((sum, item) => sum + item.compute(), 0));
