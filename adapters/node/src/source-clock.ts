import type { HrTime } from '@opentelemetry/api';

const monotonicClock = process.hrtime.bigint.bind(process.hrtime);
const epochClock = Date.now.bind(Date);
const epochOffset = BigInt(epochClock()) * 1_000_000n - monotonicClock();

/** Capture the producer's clocks before application code can replace its own clocks. */
export function monotonicNs(): bigint { return monotonicClock(); }

/** Record the observed wall-clock origin; duration uses the separate monotonic clock. */
export function epochNs(monotonic = monotonicClock()): bigint { return epochOffset + monotonic; }

/** Translate integer nanoseconds to the SDK's public timestamp representation. */
export function sdkTime(nanoseconds: bigint): HrTime {
  return [Number(nanoseconds / 1_000_000_000n), Number(nanoseconds % 1_000_000_000n)];
}
