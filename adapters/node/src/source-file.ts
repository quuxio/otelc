import { closeSync, constants, fstatSync, openSync, readFileSync, statSync } from 'node:fs';

/** Read one stable regular file through its handle; a FIFO cannot block before validation. */
export function readStableFile(filename: string): Buffer {
  const descriptor = openSync(filename, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const before = fstatSync(descriptor, { bigint: true });
    if (!before.isFile()) throw new Error('loaded input is not a regular file');
    const bytes = readFileSync(descriptor);
    const after = fstatSync(descriptor, { bigint: true });
    const current = statSync(filename, { bigint: true });
    const stable = (value: typeof before): string => [value.dev, value.ino, value.size, value.mtimeNs, value.ctimeNs].join(':');
    if (stable(before) !== stable(after) || stable(after) !== stable(current)) throw new Error('loaded input changed during read');
    return bytes;
  } finally { closeSync(descriptor); }
}
