import { realpathSync } from 'node:fs';
import { registerHooks } from 'node:module';
import type { ModuleSource } from 'node:module';
import { fileURLToPath } from 'node:url';
import { transform } from './compiler/transform.mjs';
import { transpile } from './compiler/typescript.mjs';
import { LocalMetricControl } from './local-metric-control.ts';
import { Selection, sourceName } from './selection.ts';
import { RUNTIME, SourceRuntime } from './source-runtime.ts';
import type { ISourcePlan } from './source-plan.ts';
import { SourceInputs } from './source-inputs.ts';

const producerRoot = fileURLToPath(new URL('./', import.meta.url));
const globals = globalThis as typeof globalThis & { [RUNTIME]?: SourceRuntime };

function sourceText(source: ModuleSource): string {
  if (typeof source === 'string') return source;
  return new TextDecoder('utf-8', { ignoreBOM: true }).decode(source);
}

/** Own a loader, SDK and controls; transformed modules retain their original execution URLs. */
export function installSource(plan: ISourcePlan): {
  runtime: SourceRuntime; control: LocalMetricControl; close(): Promise<void>;
} {
  if (globals[RUNTIME]) throw new Error('otelc source instrumentation is already installed');
  const root = realpathSync(plan.runtime.root);
  const inputs = new SourceInputs(plan);
  const selection = new Selection(plan.source_matchers, true);
  const runtime = SourceRuntime.start(plan);
  let control: LocalMetricControl;
  try { control = LocalMetricControl.start(plan.runtime.directory, runtime); }
  catch (error) { void runtime.shutdown().catch(() => {}); throw error; }
  void control.ready.catch(() => runtime.lose('invalid'));
  Object.defineProperty(globals, RUNTIME, { value: runtime, configurable: true });
  const hook = registerHooks({ load(url, context, nextLoad) {
    if (!url.startsWith('file:')) return nextLoad(url, context);
    const filename = realpathSync(fileURLToPath(url));
    const name = sourceName(filename, root);
    if (!name || filename.startsWith(producerRoot)) return nextLoad(url, context);
    if (/\.(?:ts|mts|cts)$/.test(filename)) {
      const loadIdentity = inputs.beginModule(filename, name, url, 'typescript');
      const selected = selection.accepts(name);
      const execution = selected ? plan : { ...plan, plain: true, functions: { include: [], exclude: [] },
        function_matchers: { include: [], exclude: [] }, lifetimes: { ...plan.lifetimes, enabled: false } };
      const prepared = transpile(inputs.source(filename), filename, name, execution, root,
        context.format?.replace('-typescript', ''), inputs.config);
      inputs.compilation(loadIdentity, filename, prepared.compilation);
      inputs.emitted(loadIdentity, filename, name, url, prepared.code, prepared.format, 'compiled', prepared.map);
      if (!selected) {
        const source = prepared.code + '\n//# sourceMappingURL=data:application/json;base64,' + Buffer.from(JSON.stringify(prepared.map)).toString('base64');
        inputs.emitted(loadIdentity, filename, name, url, source, prepared.format ?? 'module', 'loaded', prepared.map);
        return { format: prepared.format ?? 'module', shortCircuit: true, source };
      }
      const output = transform(prepared.code, filename, name, execution, runtime, prepared);
      inputs.emitted(loadIdentity, filename, name, url, output.code, output.format, 'instrumented');
      return { format: output.format, shortCircuit: true, source: output.code };
    }
    const loaded = nextLoad(url, context);
    if (!/\.(?:mjs|cjs|js)$/.test(filename)) return loaded;
    const loadIdentity = inputs.beginModule(filename, name, url, 'javascript');
    const source = inputs.source(filename, loaded.source === undefined || loaded.source === null
      ? undefined : sourceText(loaded.source));
    if (!selection.accepts(name)) {
      inputs.emitted(loadIdentity, filename, name, url, source, loaded.format ?? undefined, 'loaded');
      return loaded;
    }
    const output = transform(source, filename, name, plan, runtime).code;
    inputs.emitted(loadIdentity, filename, name, url, output, loaded.format ?? undefined, 'instrumented');
    return { ...loaded, source: output };
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
