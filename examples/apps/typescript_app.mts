interface Item { value: number; }
type Identity<T> = T;
export function process_order<T extends Item>(item: Identity<T>): number { return item.value * 3; }
function recursive(depth: number): number { return depth ? 1 + recursive(depth - 1) : 0; }
function caught(): number { try { throw new Error('caught'); } catch { return 7; } }
function throws(): never { throw new Error('escaping'); }
function* values(): Generator<number> { yield 1; yield 2; }
async function delayed(value: number): Promise<number> { await Promise.resolve(); return process_order({ value }); }
async function rejected(): Promise<never> { await Promise.resolve(); throw new Error('rejected'); }
enum State { Open = 4, Closed = 8 }
namespace Rules {
  export function bonus(value: number): number { return value + 1; }
}
export class Order {
  constructor(public value: number) {}
  calculate(globalThis: number, Symbol: number): number { return this.value + globalThis + Symbol; }
  #private(): number { return 5; }
  privateValue(): number { return this.#private(); }
}
function main(): number { return process_order({ value: 4 }) + recursive(3) + caught(); }
let result: number = main();
try { throws(); } catch (error) { result += (error as Error).message.length; }
result += [...values()].reduce((sum: number, value: number): number => sum + value, 0);
result += await delayed(5);
try { await rejected(); } catch (error) { result += (error as Error).message.length; }
const order: Order = new Order(State.Open);
result += order.calculate(2, 3) + order.privateValue() + Rules.bonus(4);
console.log(result);
