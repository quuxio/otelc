// Diagnostic SDK/control fixture, not a source-free application example.
import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { ValueType } from '@opentelemetry/api';
import { OwnedSdkPipeline } from '../../src/sdk-pipeline.ts';
import type { ISdkPolicy } from '../../src/sdk-pipeline.ts';
import { LocalMetricControl } from '../../src/local-metric-control.ts';

const [, , policyFile, runtime] = process.argv;
const policy = JSON.parse(readFileSync(policyFile, 'utf8')) as ISdkPolicy;
const pipeline = new OwnedSdkPipeline(policy);
const controls = await LocalMetricControl.listen(runtime, pipeline);
const meter = pipeline.meterProvider.getMeter('otelc.sdk-fixture', '0.2.0');
const calls = meter.createCounter('otelc.function.calls', { valueType: ValueType.INT });
const duration = meter.createHistogram('otelc.function.duration', { unit: 's' });
let callbackCalls = 0;
meter.createObservableGauge('otelc.fixture.callback').addCallback(result => {
  callbackCalls++; result.observe(callbackCalls);
});
const tracer = pipeline.tracerProvider.getTracer('otelc.sdk-fixture', '0.2.0');
const input = createInterface({ input: process.stdin, terminal: false });
for await (const line of input) {
  if (line === 'quit') break;
  if (line === 'callbacks') {
    process.stdout.write(JSON.stringify({ callback_calls: callbackCalls }) + '\n');
    continue;
  }
  const count = Number(line);
  tracer.startSpan('SDK fixture batch').end();
  calls.add(count, { function: 'cached' });
  duration.record(.005, { function: 'cached' });
  process.stdout.write(JSON.stringify({ checksum: count, pid: process.pid }) + '\n');
}
input.close();
process.stdin.pause();
await controls.close();
try { await pipeline.shutdown(); }
finally { writeFileSync(path.join(runtime, 'sdk-status.json'), JSON.stringify(pipeline.status()) + '\n', { mode: 0o600 }); }
