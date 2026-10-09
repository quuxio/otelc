import fs from 'node:fs';
import path from 'node:path';
import { registerHooks } from 'node:module';
import { transform } from './transform.mjs';
import { Selection, sourceName } from './policy.mjs';
import { transpile, compilerOptions } from './typescript.mjs';
import { nativeObserver } from './promise-observer.mjs';
import ts from 'typescript';
import { nativeVersion } from './typescript-native.mjs';

export function inspect(filename, plan, root = process.cwd()) {
  const name = sourceName(path.resolve(filename), root);
  if (!name || !new Selection(plan.source_matchers).accepts(name)) return { language: plan.language, functions: [] };
  const source = fs.readFileSync(filename, 'utf8');
  const prepared = plan.language === 'typescript' && /\.(?:ts|mts|cts|tsx)$/.test(filename) ? transpile(source, path.resolve(filename), name, plan, root) : null;
  return { language: plan.language, functions: transform(prepared?.code ?? source, path.resolve(filename), name, plan, { register: () => true }, prepared).functions };
}
if (process.argv[1] === new URL(import.meta.url).pathname) {
  const [, , planPath, command, ...args] = process.argv;
  const plan = JSON.parse(fs.readFileSync(planPath, 'utf8'));
  if (command === '--doctor') {
    if (plan.language === 'typescript') compilerOptions();
    if (typeof registerHooks !== 'function') throw new Error('Node 24.11+ with synchronous module hooks is required');
    nativeObserver();
    const compiler = plan.language === 'typescript' ? plan.backend === 'native' ? `; native TypeScript ${nativeVersion()} emit, identity parser ${ts.version}` : `; TypeScript API ${ts.version} emit` : '';
    console.log(`Node ${process.version}${compiler}: in-memory ${plan.language} transformation and OTLP/HTTP metrics${plan.traces?.enabled ? ' and function spans' : ''} available`);
  } else if (command === '--inspect' && args.length >= 1 && args.slice(1).every(arg => arg === '--json')) {
    console.log(JSON.stringify(inspect(args[0], plan), null, 2));
  } else throw new Error('Node inspect requires SCRIPT [--json]');
}
