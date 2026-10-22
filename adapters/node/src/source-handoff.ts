import { constants, closeSync, fstatSync, openSync, writeSync } from 'node:fs';
import { serialize, deserialize } from 'node:v8';
import { monotonicNs } from './source-clock.ts';

// POSIX guarantees atomic FIFO writes of at least 512 bytes. Each independently
// framed chunk fits that minimum; a dropped record cannot corrupt the next one.
const MAGIC = 0x4f544c43;
const HEADER = 14;
const PAYLOAD = 512 - HEADER;
const MAX_RECORD = 16 * 1024 * 1024;
const wait = Atomics.wait.bind(Atomics);
const sleeper = new Int32Array(new SharedArrayBuffer(4));

/** Bounded synchronous handoff; this owns no event-loop handle or application callback. */
export class SourceHandoffWriter {
  readonly #descriptor: number;
  #next = 1;
  #closed = false;
  attempted = 0;
  accepted = 0;
  lost = 0;

  constructor(filename: string) {
    this.#descriptor = openSync(filename, constants.O_WRONLY | constants.O_NONBLOCK | constants.O_NOFOLLOW);
    const metadata = fstatSync(this.#descriptor);
    if (!metadata.isFIFO() || metadata.uid !== process.getuid?.() || (metadata.mode & 0o7777) !== 0o600) {
      closeSync(this.#descriptor);
      throw new Error('source handoff must be an owned private FIFO');
    }
  }

  send(value: object): boolean {
    this.attempted++;
    if (this.#closed || this.#next > 0xffffffff) { this.lost++; return false; }
    try {
      const data = serialize(value);
      if (data.length > MAX_RECORD) { this.lost++; return false; }
      const id = this.#next++;
      const total = Math.ceil(data.length / PAYLOAD);
      const deadline = monotonicNs() + 50_000_000n;
      for (let part = 0; part < total; part++) {
        const payload = data.subarray(part * PAYLOAD, (part + 1) * PAYLOAD);
        const chunk = Buffer.allocUnsafe(HEADER + payload.length);
        chunk.writeUInt32BE(MAGIC, 0); chunk.writeUInt32BE(id, 4);
        chunk.writeUInt16BE(part, 8); chunk.writeUInt16BE(total, 10); chunk.writeUInt16BE(payload.length, 12);
        payload.copy(chunk, HEADER);
        this.#writeChunk(chunk, deadline);
      }
      this.accepted++;
      return true;
    } catch { this.lost++; return false; }
  }

  #writeChunk(chunk: Buffer, deadline: bigint): void {
    for (;;) {
      try {
        if (writeSync(this.#descriptor, chunk) !== chunk.length) { this.close(); throw new Error('partial atomic write'); }
        return;
      } catch (error) {
        if (!(error instanceof Error) || !('code' in error) || error.code !== 'EAGAIN'
            || monotonicNs() >= deadline) throw error;
        wait(sleeper, 0, 0, 1);
      }
    }
  }

  status() { return { attempted: this.attempted, accepted: this.accepted, lost: this.lost, deadline_ms: 50 }; }
  close(): void { if (!this.#closed) { this.#closed = true; closeSync(this.#descriptor); } }
}

/** Recover complete private records; partial handoffs are counted, never exported as complete. */
export class SourceHandoffReader {
  #buffer = Buffer.alloc(0);
  #id = 0;
  #part = 0;
  #total = 0;
  #size = 0;
  #record: Buffer[] = [];
  lost = 0;
  accepted = 0;

  push(data: Buffer): object[] {
    this.#buffer = Buffer.concat([this.#buffer, data]);
    const messages: object[] = [];
    while (this.#buffer.length >= HEADER) {
      const chunk = this.#buffer;
      const id = chunk.readUInt32BE(4), part = chunk.readUInt16BE(8), total = chunk.readUInt16BE(10);
      const length = chunk.readUInt16BE(12);
      if (chunk.readUInt32BE(0) !== MAGIC || !total || part >= total || length > PAYLOAD) {
        throw new Error('invalid source handoff framing');
      }
      if (chunk.length < HEADER + length) break;
      this.#buffer = chunk.subarray(HEADER + length);
      if (id !== this.#id) this.#beginRecord(id, total);
      if (part !== this.#part || total !== this.#total || this.#size + length > MAX_RECORD) {
        throw new Error('invalid source handoff record');
      }
      this.#record.push(chunk.subarray(HEADER, HEADER + length)); this.#size += length; this.#part++;
      if (this.#part !== total) continue;
      const value: unknown = deserialize(Buffer.concat(this.#record, this.#size));
      if (!value || typeof value !== 'object') throw new Error('invalid source handoff value');
      messages.push(value); this.accepted++; this.#record = [];
    }
    return messages;
  }

  #beginRecord(id: number, total: number): void {
    if (this.#record.length) this.lost++;
    this.#id = id; this.#part = 0; this.#total = total; this.#size = 0; this.#record = [];
  }

  finish(): void { if (this.#buffer.length || this.#record.length) this.lost++; }
}
