import { readFileSync, realpathSync } from 'node:fs';
import { registerHooks } from 'node:module';
import { fileURLToPath } from 'node:url';
import { transpile } from './compiler/typescript.mjs';
import { sourceName } from './selection.ts';
import type { ISourcePlan } from './source-plan.ts';
import { SourceInputs } from './source-inputs.ts';

const filename = process.env.OTELC_NODE_PLAN;
if (!filename) throw new Error('otelc plain compiler loader requires a generated plan');
const plan = JSON.parse(readFileSync(filename, 'utf8')) as ISourcePlan;
delete process.env.OTELC_NODE_PLAN;
plan.plain = true;
const inputs = new SourceInputs(plan);
const root = realpathSync(plan.runtime.root);
const producerRoot = fileURLToPath(new URL('./', import.meta.url));
registerHooks({ load(url, context, nextLoad) {
  if (!url.startsWith('file:')) return nextLoad(url, context);
  const file = realpathSync(fileURLToPath(url));
  const name = sourceName(file, root);
  if (!name || file.startsWith(producerRoot) || !/\.(?:ts|mts|cts)$/.test(file)) return nextLoad(url, context);
  const loadIdentity = inputs.beginModule(file, name, url, 'typescript');
  const output = transpile(inputs.source(file), file, name, plan, root, context.format?.replace('-typescript', ''), inputs.config);
  inputs.compilation(loadIdentity, output.compilation);
  inputs.emitted(loadIdentity, output.code, output.format, 'compiled', output.map);
  const source = output.code + '\n//# sourceMappingURL=data:application/json;base64,' + Buffer.from(JSON.stringify(output.map)).toString('base64');
  inputs.emitted(loadIdentity, source, output.format ?? 'module', 'loaded', output.map);
  return { format: output.format ?? 'module', shortCircuit: true,
    source };
} });
