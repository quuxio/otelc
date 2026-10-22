import { createInterface } from 'node:readline';

class WorkItem {
  readonly seed: number;
  constructor(seed: number) { this.seed = seed; }
  close(): void { /* This example owns no external resource. */ }
}

class LifecycleWorkItem {
  private failClose: boolean;
  constructor(seed: number) {
    if (seed < 0) throw new RangeError('construction failed');
    this.failClose = seed === 1;
  }
  close(): void {
    if (this.failClose) { this.failClose = false; throw new Error('close failed'); }
  }
}

function lifecycle(): void {
  let constructionFailed = false;
  let closeFailed = false;
  try { new LifecycleWorkItem(-1); }
  catch { constructionFailed = true; }
  const item = new LifecycleWorkItem(1);
  try { item.close(); }
  catch { closeFailed = true; }
  item.close();
  item.close();
  new LifecycleWorkItem(0);
  console.log(JSON.stringify({ construction_failed: constructionFailed, close_failed: closeFailed }));
}

function workload(seed: number): number {
  if (seed < 0) throw new RangeError('negative seed');
  const item = new WorkItem(seed);
  try {
    let value = item.seed;
    let sum = 0;
    for (let step = 0; step < 128; step++) {
      value = (value * 1664525 + 1013904223) & 65535;
      sum += value;
    }
    return sum;
  } finally { item.close(); }
}

function excluded(seed: number): number { return seed; }

const input = createInterface({ input: process.stdin, terminal: false });
for await (const line of input) {
  if (line === 'quit') break;
  if (line === 'lifecycle') { lifecycle(); continue; }
  if (line === 'throw') {
    try { workload(-1); }
    catch (error: unknown) {
      if (!(error instanceof Error)) throw error;
      console.log(JSON.stringify({ exception: error.name, message: error.message }));
    }
    continue;
  }
  if (line === 'gc') {
    globalThis.gc?.();
    console.log(JSON.stringify({ gc_requested: typeof globalThis.gc === 'function' }));
    continue;
  }
  const count = Number(line);
  if (!Number.isInteger(count) || count < 1 || count > 1_000_000) throw new RangeError('count out of range');
  const start = process.hrtime.bigint();
  let checksum = 0;
  for (let index = 0; index < count; index++) checksum += workload(index) + excluded(0);
  const elapsed = process.hrtime.bigint() - start;
  console.log(JSON.stringify({ checksum, elapsed_ns: Number(elapsed), calls: count }));
}
input.close();
process.stdin.pause();
