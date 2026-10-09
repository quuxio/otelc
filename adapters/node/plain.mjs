// Compiler-only baseline: the same TypeScript emit with no SDK or timing probes.
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { registerHooks } from 'node:module';
import { transpile } from './typescript.mjs';
import { sourceName } from './policy.mjs';
import { transform } from './transform.mjs';

const root = fs.realpathSync(process.cwd());
const backend = process.env.OTELC_TYPESCRIPT_BACKEND ?? 'source';
if (!['source', 'native'].includes(backend)) throw new Error('unsupported compiler-only TypeScript backend');
registerHooks({ load(url, context, nextLoad) {
  if (!url.startsWith('file:')) return nextLoad(url, context);
  const filename = fs.realpathSync(fileURLToPath(url));
  const name = sourceName(filename, root);
  if (!name || !/\.(?:ts|mts|cts|tsx)$/.test(filename)) return nextLoad(url, context);
  const plan = { backend, annotations: { read_existing: false }, function_matchers: { include: [], exclude: [] } };
  const prepared = transpile(fs.readFileSync(filename, 'utf8'), filename, name, plan, root);
  const hinted = context.format?.replace('-typescript', '');
  if (['module', 'commonjs'].includes(hinted)) prepared.format = hinted;
  const output = transform(prepared.code, filename, name, plan, { register: () => false }, prepared);
  return { format: output.format, source: output.code, shortCircuit: true };
} });
