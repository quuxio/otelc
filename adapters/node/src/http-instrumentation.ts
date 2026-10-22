import { createNoopMeter, propagation } from '@opentelemetry/api';
import type { MeterProvider } from '@opentelemetry/api';
import { CompositePropagator, W3CBaggagePropagator, W3CTraceContextPropagator } from '@opentelemetry/core';
import { registerInstrumentations } from '@opentelemetry/instrumentation';
import { createRequire, syncBuiltinESMExports } from 'node:module';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import type { SourceSdk } from './source-sdk.ts';

const require = createRequire(import.meta.url);
const producerRoot = fileURLToPath(new URL('../../../', import.meta.url));

interface IHttpProduct {
  schema_version: number; upstream_version: string; patch_version: number; directory: string;
  helper_sha256: string; files: Record<string, string>;
  upstream_sha256: Record<string, string>;
}
export interface IHttpInstallation {
  identity: { upstream_version: string; patch_version: number; product: string };
  stop(): void;
}

function digest(filename: string): string { return createHash('sha256').update(readFileSync(filename)).digest('hex'); }

// Protocol fingerprints use Python-compatible code-unit order for these ASCII keys.
// Locale collation would change identities (for example LICENSE versus build/src).
function compareIdentityKeys(left: string, right: string): number {
  if (left === right) return 0;
  return left < right ? -1 : 1;
}

/** Refuse stale/corrupt generated products before any application or upstream patch executes. */
export function loadHttpProduct(root: string): { directory: string; manifest: IHttpProduct } {
  const manifest = JSON.parse(readFileSync(path.join(root, 'build/tools/otelc-http.json'), 'utf8')) as IHttpProduct;
  if (manifest.schema_version !== 1 || manifest.upstream_version !== '0.223.0' || manifest.patch_version !== 1
      || !/^otelc-http-0\.223\.0-[a-f0-9]{16}$/.test(manifest.directory)
      || manifest.helper_sha256 !== digest(path.join(root, 'adapters/node/src/http-preservation.ts'))) {
    throw new Error('rebuild the qualified HTTP instrumentation product with make node-http');
  }
  const directory = path.join(root, 'build/tools', manifest.directory);
  const expected = ['LICENSE', 'package.json', ...['http', 'index', 'internal-types', 'semconv', 'types', 'utils', 'version']
    .map(name => 'build/src/' + name + '.js')].sort(compareIdentityKeys);
  if (Object.keys(manifest.files).sort(compareIdentityKeys).join('\n') !== expected.join('\n')
      || expected.some(filename => !/^[a-f0-9]{64}$/.test(manifest.files[filename])
        || digest(path.join(directory, filename)) !== manifest.files[filename])) {
    throw new Error('qualified HTTP instrumentation product checksum mismatch');
  }
  const sorted = (record: Record<string, string>): Record<string, string> =>
    Object.fromEntries(Object.keys(record).sort(compareIdentityKeys).map(key => [key, record[key]]));
  const canonical = JSON.stringify({ files: sorted(manifest.files), helper_sha256: manifest.helper_sha256,
    patch_version: manifest.patch_version, schema_version: manifest.schema_version,
    upstream_sha256: sorted(manifest.upstream_sha256), upstream_version: manifest.upstream_version });
  const identity = createHash('sha256').update(canonical).digest('hex').slice(0, 16);
  if (manifest.directory !== 'otelc-http-0.223.0-' + identity) {
    throw new Error('qualified HTTP instrumentation product identity mismatch');
  }
  return { directory, manifest };
}

/** The pinned official HTTP instrumentor produces synchronous duration histograms. */
function httpMeterProvider(sdk: SourceSdk): MeterProvider {
  return { getMeter(name, version, options) {
    const meter = createNoopMeter();
    const source = sdk.metrics(name, version, options);
    meter.createHistogram = (instrument, settings) => source.histogram(instrument, settings);
    return meter;
  } };
}

/** Common context.http drives official node:http/node:https instrumentation and W3C propagation. */
export function installHttp(sdk: SourceSdk): IHttpInstallation {
  const product = loadHttpProduct(producerRoot);
  // This optional, generated preservation patch is loaded only for context.http. It retains
  // the official public API and all unmodified runtime files bound in its manifest.
  const { HttpInstrumentation } = require(path.join(product.directory, 'build/src/index.js')) as
    typeof import('@opentelemetry/instrumentation-http');
  const propagator = new CompositePropagator({ propagators: [new W3CTraceContextPropagator(), new W3CBaggagePropagator()] });
  if (!propagation.setGlobalPropagator(propagator)) throw new Error('an OpenTelemetry propagator is already installed');
  let stop: (() => void) | undefined;
  try {
    stop = registerInstrumentations({ tracerProvider: sdk.tracerProvider, meterProvider: httpMeterProvider(sdk),
      instrumentations: [new HttpInstrumentation({ enabled: false })] });
    // Load and patch core exports before either app module system captures them.
    require('node:http'); require('node:https'); syncBuiltinESMExports();
  } catch (error) { stop?.(); propagation.disable(); throw error; }
  return { identity: { upstream_version: product.manifest.upstream_version,
    patch_version: product.manifest.patch_version, product: product.manifest.directory },
    stop() { stop?.(); syncBuiltinESMExports(); propagation.disable(); } };
}
