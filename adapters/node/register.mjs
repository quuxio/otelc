import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { registerHooks } from 'node:module';
import { Runtime, RUNTIME } from './runtime.mjs';
import { Selection, sourceName } from './policy.mjs';
import { transform } from './transform.mjs';

export async function install(plan, root = process.cwd()) {
  root = fs.realpathSync(root);
  if (!['javascript', 'typescript'].includes(plan.language) || !plan.execution_available) throw new Error('Node adapter requires an executable JavaScript or TypeScript policy');
  if (globalThis[RUNTIME]) throw new Error('Node instrumentation is already installed');
  const typed = plan.language === 'typescript' ? await import('./typescript.mjs') : null;
  typed?.compilerOptions(root);
  const runtime = new Runtime(plan);
  try { await runtime.bindControl(); }
  catch (error) { await runtime.close(); throw error; }
  Object.defineProperty(globalThis, RUNTIME, { value: runtime, configurable: true });
  const sources = new Selection(plan.source_matchers);
  const protectedRoot = path.dirname(fileURLToPath(import.meta.url));
  const hook = registerHooks({ load(url, context, nextLoad) {
    if (!url.startsWith('file:')) return nextLoad(url, context);
    const filename = fs.realpathSync(fileURLToPath(url));
    const name = sourceName(filename, root);
    if (!name || filename.startsWith(protectedRoot + path.sep)) return nextLoad(url, context);
    if (typed && /\.(?:ts|mts|cts|tsx)$/.test(filename)) {
      const source = fs.readFileSync(filename, 'utf8');
      const selected = sources.accepts(name);
      const prepared = typed.transpile(source, filename, name, selected ? plan : { ...plan, annotations: { ...plan.annotations, read_existing: false } }, root);
      const hinted = context.format?.replace('-typescript', '');
      if (['module', 'commonjs'].includes(hinted)) prepared.format = hinted;
      const executionPlan = selected ? plan : { ...plan,
        function_matchers: { include: [], exclude: [] }, annotations: { ...plan.annotations, read_existing: false } };
      const output = transform(prepared.code, filename, name, executionPlan, runtime, prepared);
      return { format: output.format, shortCircuit: true, source: output.code };
    }
    const result = nextLoad(url, context);
    if (!sources.accepts(name) || !/\.(?:mjs|cjs|js)$/.test(filename)) return result;
    const source = result.source === null || result.source === undefined ? fs.readFileSync(filename, 'utf8') : result.source.toString();
    return { ...result, source: transform(source, filename, name, plan, runtime).code };
  } });
  const close = async () => { hook.deregister(); await runtime.close(); delete globalThis[RUNTIME]; };
  return { runtime, close };
}
if (process.env.OTELC_NODE_PLAN) {
  const plan = JSON.parse(fs.readFileSync(process.env.OTELC_NODE_PLAN, 'utf8'));
  delete process.env.OTELC_NODE_PLAN;
  const application = await install(plan);
  let closing = false;
  process.on('beforeExit', () => {
    if (closing) return;
    closing = true;
    void application.close().catch(() => { application.runtime.exportLoss++; application.runtime.report(); });
  });
  process.on('exit', () => application.runtime.report());
}
