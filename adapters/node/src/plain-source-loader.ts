import { readFileSync, realpathSync } from 'node:fs';
import { registerHooks } from 'node:module';
import { fileURLToPath } from 'node:url';
import { transpile } from './compiler/typescript.mjs';
import { sourceName } from './selection.ts';
import type { ISourcePlan } from './source-plan.ts';

const filename = process.env.OTELC_NODE_PLAN;
if (!filename) throw new Error('otelc plain compiler loader requires a generated plan');
const plan = JSON.parse(readFileSync(filename, 'utf8')) as ISourcePlan;
delete process.env.OTELC_NODE_PLAN;
plan.plain = true;
const root = realpathSync(plan.runtime.root);
const producerRoot = fileURLToPath(new URL('./', import.meta.url));
registerHooks({ load(url, context, nextLoad) {
  if (!url.startsWith('file:')) return nextLoad(url, context);
  const file = realpathSync(fileURLToPath(url));
  const name = sourceName(file, root);
  if (!name || file.startsWith(producerRoot) || !/\.(?:ts|mts|cts)$/.test(file)) return nextLoad(url, context);
  const output = transpile(readFileSync(file, 'utf8'), file, name, plan, root, context.format?.replace('-typescript', ''));
  return { format: output.format ?? 'module', shortCircuit: true,
    source: output.code + '\n//# sourceMappingURL=data:application/json;base64,' + Buffer.from(JSON.stringify(output.map)).toString('base64') };
} });
