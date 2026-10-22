import { realpathSync, readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
import { fileURLToPath } from 'node:url';
import { transform } from './compiler/transform.mjs';
import { transpile } from './compiler/typescript.mjs';
import { LocalMetricControl } from './local-metric-control.ts';
import { Selection, sourceName } from './selection.ts';
import { RUNTIME, SourceRuntime } from './source-runtime.ts';
import type { ISourcePlan } from './source-plan.ts';

const producerRoot = fileURLToPath(new URL('./', import.meta.url));
const globals = globalThis as typeof globalThis & { [RUNTIME]?: SourceRuntime };

/** Own a loader, SDK and controls; transformed modules retain their original execution URLs. */
export async function installSource(plan: ISourcePlan): Promise<{
  runtime: SourceRuntime; control: LocalMetricControl; close(): Promise<void>;
}> {
  if (globals[RUNTIME]) throw new Error('otelc source instrumentation is already installed');
  const root = realpathSync(plan.runtime.root);
  const selection = new Selection(plan.source_matchers, true);
  const runtime = await SourceRuntime.start(plan);
  let control: LocalMetricControl;
  try { control = await LocalMetricControl.listen(plan.runtime.directory, runtime); }
  catch (error) { await runtime.shutdown(); throw error; }
  runtime.sdk.descriptor(control.cleanupDescriptor());
  Object.defineProperty(globals, RUNTIME, { value: runtime, configurable: true });
  const hook = registerHooks({ load(url, context, nextLoad) {
    if (!url.startsWith('file:')) return nextLoad(url, context);
    const filename = realpathSync(fileURLToPath(url));
    const name = sourceName(filename, root);
    if (!name || filename.startsWith(producerRoot)) return nextLoad(url, context);
    if (/\.(?:ts|mts|cts)$/.test(filename)) {
      const selected = selection.accepts(name);
      const execution = selected ? plan : { ...plan, plain: true, functions: { include: [], exclude: [] },
        function_matchers: { include: [], exclude: [] }, lifetimes: { ...plan.lifetimes, enabled: false } };
      const prepared = transpile(readFileSync(filename, 'utf8'), filename, name, execution, root,
        context.format?.replace('-typescript', ''));
      if (!selected) return { format: prepared.format ?? 'module', shortCircuit: true,
        source: prepared.code + '\n//# sourceMappingURL=data:application/json;base64,' + Buffer.from(JSON.stringify(prepared.map)).toString('base64') };
      const output = transform(prepared.code, filename, name, execution, runtime, prepared);
      return { format: output.format, shortCircuit: true, source: output.code };
    }
    const loaded = nextLoad(url, context);
    if (!selection.accepts(name) || !/\.(?:mjs|cjs|js)$/.test(filename)) return loaded;
    const source = loaded.source === undefined || loaded.source === null ? readFileSync(filename, 'utf8')
      : typeof loaded.source === 'string' ? loaded.source : new TextDecoder('utf-8', { ignoreBOM: true }).decode(loaded.source);
    return { ...loaded, source: transform(source, filename, name, plan, runtime).code };
  } });
  let closing: Promise<void> | undefined;
  return { runtime, control, close() {
    closing ??= (async () => {
      hook.deregister();
      try { await control.close(); }
      finally {
        delete globals[RUNTIME];
        await runtime.shutdown();
      }
    })();
    return closing;
  } };
}
