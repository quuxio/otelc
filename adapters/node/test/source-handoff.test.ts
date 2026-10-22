import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { constants, closeSync, mkdtempSync, openSync, readSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { SourceHandoffReader, SourceHandoffWriter } from '../src/source-handoff.ts';

interface IFixture { writer: SourceHandoffWriter; read(): Buffer; closeReader(): void; directory: string; close(): void }

function fixture(): IFixture {
  const directory = mkdtempSync(path.join(tmpdir(), 'otelc-handoff-'));
  const filename = path.join(directory, 'input.fifo');
  execFileSync('mkfifo', ['-m', '600', filename]);
  const descriptor = openSync(filename, constants.O_RDONLY | constants.O_NONBLOCK);
  const writer = new SourceHandoffWriter(filename);
  let readerClosed = false;
  return { directory, writer, read() {
    const chunks: Buffer[] = [];
    for (;;) {
      const data = Buffer.alloc(4096);
      try {
        const bytes = readSync(descriptor, data);
        if (!bytes) break;
        chunks.push(data.subarray(0, bytes));
      } catch (error) {
        if (error instanceof Error && 'code' in error && error.code === 'EAGAIN') break;
        throw error;
      }
    }
    return Buffer.concat(chunks);
  }, closeReader() { if (!readerClosed) { readerClosed = true; closeSync(descriptor); } }, close() {
    writer.close(); this.closeReader(); rmSync(directory, { recursive: true, force: true });
  } };
}

test('real FIFO records survive byte-fragmented reads with primitive identities intact', t => {
  const input = fixture(); t.after(() => input.close());
  const records = [{ name: 'workload', timestamp: 123456789012345678n }, { payload: 'x'.repeat(3000) }];
  for (const record of records) assert.equal(input.writer.send(record), true);
  const reader = new SourceHandoffReader();
  const observed = [...input.read()].flatMap(byte => reader.push(Buffer.from([byte])));
  reader.finish();
  assert.deepEqual(observed, records);
  assert.equal(reader.accepted, 2); assert.equal(reader.lost, 0);
  assert.deepEqual(input.writer.status(), { attempted: 2, accepted: 2, lost: 0, deadline_ms: 50 });
});

test('a blocked real FIFO is bounded and the next record recovers without fabricating completion', t => {
  const input = fixture(); t.after(() => input.close());
  const reader = new SourceHandoffReader();
  const start = process.hrtime.bigint();
  assert.equal(input.writer.send({ payload: 'x'.repeat(1024 * 1024) }), false);
  assert.ok(process.hrtime.bigint() - start < 1_000_000_000n, 'a stopped SDK reader must not block application indefinitely');
  assert.deepEqual(reader.push(input.read()), []);
  assert.equal(input.writer.send({ recovered: true }), true);
  assert.deepEqual(reader.push(input.read()), [{ recovered: true }]);
  reader.finish();
  assert.equal(reader.lost, 1); assert.equal(reader.accepted, 1);
  assert.equal(input.writer.status().lost, 1);
});

test('EOF records a partially handed-off observation as loss', t => {
  const input = fixture(); t.after(() => input.close());
  assert.equal(input.writer.send({ payload: 'x'.repeat(1024 * 1024) }), false);
  const reader = new SourceHandoffReader();
  assert.deepEqual(reader.push(input.read()), []);
  reader.finish();
  assert.equal(reader.lost, 1); assert.equal(reader.accepted, 0);
});

test('oversized or closed producer records cannot become handoff success', t => {
  const input = fixture(); t.after(() => input.close());
  assert.equal(input.writer.send({ payload: Buffer.alloc(16 * 1024 * 1024) }), false);
  assert.equal(input.read().length, 0);
  assert.equal(input.writer.send({ normal: true }), true);
  input.writer.close(); input.writer.close();
  assert.equal(input.writer.send({ afterClose: true }), false);
  assert.equal(input.writer.status().lost, 2);
});

test('reader death becomes loss without throwing into application code', t => {
  const input = fixture(); t.after(() => input.close()); input.closeReader();
  assert.equal(input.writer.send({ afterReaderExit: true }), false);
  assert.equal(input.writer.status().lost, 1);
});

test('regular files, symlinks and permissive FIFOs are refused', t => {
  const input = fixture(); t.after(() => input.close());
  const regular = path.join(input.directory, 'regular');
  writeFileSync(regular, '', { mode: 0o600 });
  assert.throws(() => new SourceHandoffWriter(regular), /private FIFO/);
  const link = path.join(input.directory, 'link'); symlinkSync(regular, link);
  assert.throws(() => new SourceHandoffWriter(link));
  const permissive = path.join(input.directory, 'permissive');
  execFileSync('mkfifo', ['-m', '644', permissive]);
  const descriptor = openSync(permissive, constants.O_RDONLY | constants.O_NONBLOCK);
  try { assert.throws(() => new SourceHandoffWriter(permissive), /private FIFO/); }
  finally { closeSync(descriptor); }
});

test('corrupt private stream headers are refused rather than deserialised as telemetry', t => {
  const input = fixture(); t.after(() => input.close());
  assert.equal(input.writer.send({ normal: true }), true);
  const data = input.read(); data[0] ^= 1;
  assert.throws(() => new SourceHandoffReader().push(data), /framing/);
});
