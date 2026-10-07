export function recursive(depth: number): number { return depth ? 1 + recursive(depth - 1) : 0; }
export function child(): number { return 42; }
export function parent(): number { return child(); }
export async function asyncChild(): Promise<number> { await Promise.resolve(); return child(); }
export async function asyncParent(): Promise<number> { return asyncChild(); }
export async function returned<T>(value: T | PromiseLike<T>): Promise<T> { return value; }
export async function escaping(error: Error): Promise<never> { throw error; }

if (recursive(3) !== 3 || parent() !== 42 || await asyncParent() !== 42) throw new Error('result');
const value = {};
if (await returned(Promise.resolve(value)) !== value) throw new Error('return identity');
const error = new Error('original payload');
try { await escaping(error); throw new Error('missing exception'); }
catch (original) { if (original !== error) throw new Error('exception identity'); }
console.log('trace results preserved');
