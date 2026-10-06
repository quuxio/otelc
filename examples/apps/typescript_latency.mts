import readline from 'node:readline';
function process_order(value: number): number { return (value * 17 + 3) % 1009; }
const input = readline.createInterface({ input: process.stdin, terminal: false });
console.log('ready');
for await (const line of input) {
  if (line === 'quit') { input.close(); break; }
  const [command, value] = line.split(' ');
  const calls: number = Number(value);
  if (command !== 'batch' || !Number.isInteger(calls) || calls < 1 || calls > 1000000) throw new Error('invalid batch');
  let checksum: number = 0;
  const start: bigint = process.hrtime.bigint();
  for (let index: number = 0; index < calls; index++) checksum += process_order(index);
  console.log(`elapsed_ns=${process.hrtime.bigint() - start} checksum=${checksum} calls=${calls}`);
}
