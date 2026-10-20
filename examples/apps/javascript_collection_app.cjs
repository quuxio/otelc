class WorkItem {
  constructor(value) { this.value = value; }
  compute() { return this.value * 3; }
}
function make(value) { return new WorkItem(value); }
const items = [make(7), make(9)];
module.exports = items;
console.log(items.reduce((sum, item) => sum + item.compute(), 0));
