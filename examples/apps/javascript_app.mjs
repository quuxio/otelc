export function process_order(value) { return value * 3; }
function recursive(depth) { return depth ? 1 + recursive(depth - 1) : 0; }
function caught() { try { throw new Error('caught'); } catch { return 7; } }
function throws() { throw new Error('escaping'); }
function* values() { yield 1; yield 2; }
async function delayed(value) { await Promise.resolve(); return process_order(value); }
async function rejected() { await Promise.resolve(); throw new Error('rejected'); }
export class Order {
  constructor(value) { this.value = value; }
  calculate(globalThis, Symbol) { return this.value + globalThis + Symbol; }
  #private() { return 5; }
  privateValue() { return this.#private(); }
}
function main() { return process_order(4) + recursive(3) + caught(); }
let result = main();
try { throws(); } catch (error) { result += error.message.length; }
result += [...values()].reduce((sum, value) => sum + value, 0);
result += await delayed(5);
try { await rejected(); } catch (error) { result += error.message.length; }
const order = new Order(4);
result += order.calculate(2, 3) + order.privateValue();
console.log(result);
