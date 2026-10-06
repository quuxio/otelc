import fs from 'node:fs';
import path from 'node:path';
import { registerHooks } from 'node:module';
import { transform } from './transform.mjs';
import { Selection, sourceName } from './policy.mjs';

export function inspect(filename, plan, root = process.cwd()) {
  const name = sourceName(path.resolve(filename), root);
  if (!name || !new Selection(plan.source_matchers).accepts(name)) return { language: plan.language, functions: [] };
  return { language: plan.language, functions: transform(fs.readFileSync(filename, 'utf8'), path.resolve(filename), name, plan, { register: () => true }).functions };
}
if (process.argv[1] === new URL(import.meta.url).pathname) {
  const [, , planPath, command, ...args] = process.argv;
  const plan = JSON.parse(fs.readFileSync(planPath, 'utf8'));
  if (command === '--doctor') {
    if (typeof registerHooks !== 'function') throw new Error('Node 24.11+ with synchronous module hooks is required');
    console.log(`Node ${process.version}: in-memory JavaScript transformation and OTLP/HTTP metrics available`);
  } else if (command === '--inspect' && args.length >= 1 && args.slice(1).every(arg => arg === '--json')) {
    console.log(JSON.stringify(inspect(args[0], plan), null, 2));
  } else throw new Error('Node inspect requires SCRIPT [--json]');
}
