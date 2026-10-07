export function recursive(depth) { return depth ? 1 + recursive(depth - 1) : 0; }
export function child() { return 42; }
export function parent() { return child(); }
export async function asyncChild() { await Promise.resolve(); return child(); }
export async function asyncParent() { return asyncChild(); }
export async function returned(value) { return value; }
export async function escaping(error) { throw error; }

if (recursive(3) !== 3 || parent() !== 42 || await asyncParent() !== 42) throw new Error('result');
const value = {};
if (await returned(Promise.resolve(value)) !== value) throw new Error('return identity');
const error = new Error('original payload');
try { await escaping(error); throw new Error('missing exception'); }
catch (original) { if (original !== error) throw new Error('exception identity'); }
console.log('trace results preserved');
