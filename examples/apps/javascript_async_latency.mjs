import readline from 'node:readline';
async function process_order(value) { return Promise.resolve((value * 17 + 3) % 1009); }
const input = readline.createInterface({ input: process.stdin, terminal: false });
console.log('ready');
for await (const line of input) {
  if (line === 'quit') { input.close(); break; }
  const [command, value] = line.split(' ');
  const calls = Number(value);
  if (command !== 'batch' || !Number.isInteger(calls) || calls < 1 || calls > 1000000) throw new Error('invalid batch');
  let checksum = 0;
  const start = process.hrtime.bigint();
  for (let index = 0; index < calls; index++) checksum += await process_order(index);
  console.log(`elapsed_ns=${process.hrtime.bigint() - start} checksum=${checksum} calls=${calls}`);
}
