import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { test } from 'node:test';
import { transpile, compilerOptions } from '../typescript.mjs';
import { transform, IDENTITY } from '../transform.mjs';
import { install } from '../register.mjs';
import { inspect } from '../cli.mjs';
import { root, plan, receiver, child } from './helpers.mjs';

test('TypeScript emit preserves original identities and ignores compiler-generated helpers', () => {
  const p = { ...plan('http://127.0.0.1:1/v1/metrics'), language: 'typescript' };
  const source = `enum State { Open=4 } namespace Rules { export function bonus(n:number):number {return n+1;} }
    function decorate(value:any,context:any){return value;}
    class Order { constructor(public value:number){} @decorate method<T extends number>(value:T):number{return value+this.value;} }
    export function selected(value:number):number{return new Order(State.Open).method(value)+Rules.bonus(value);}
    const arrow=(value:number):number=>value*2;`;
  const prepared = transpile(source, '/tmp/typed.mts', 'typed.mts', p, root);
  const output = transform(prepared.code, '/tmp/typed.mts', 'typed.mts', p, { register: () => true }, prepared);
  assert.deepEqual(output.functions.map(value => value.name).sort(), ['typed.Rules.bonus', 'typed.decorate', 'typed.Order.constructor', 'typed.Order.method', 'typed.selected', 'typed.arrow'].sort());
  assert.ok(output.code.includes('sourceMappingURL')); assert.equal(prepared.map.sources[0], '/tmp/typed.mts');
  assert.equal(output.format, 'module');
  assert.throws(() => transpile(IDENTITY, '/tmp/a.mts', 'a.mts', p, root), /already prepared/);
  assert.throws(() => transpile('export function invalid(', '/tmp/a.mts', 'a.mts', p, root), /expected/);
  for (const extension of ['d.ts', 'd.mts', 'tsx']) assert.throws(() => transpile('', '/tmp/a.' + extension, 'a.' + extension, p, root), /declarations and JSX/);
});

test('typed loader preserves source, overloads, decorators, parameter properties and imported modules', async () => {
  const r = await receiver(); const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-typed-'));
  const source = `import {helper} from './helper.mts';
    function decorate(value:any,context:any){return value;}
    export class Order { constructor(public value:number){} @decorate method(n:number):number{return helper(n)+this.value;} }
    export function overloaded(value:number):number;
    export function overloaded(value:string):string;
    export function overloaded(value:number|string):number|string{return value;}
    export async function rejected():Promise<never>{await Promise.resolve();throw new Error('same');}`;
  fs.writeFileSync(path.join(directory, 'app.mts'), source);
  fs.writeFileSync(path.join(directory, 'helper.mts'), 'export enum Values { Factor=2 }\n// otelc.unknown\nexport function helper(n:number):number{return n*Values.Factor;}');
  const p = { ...plan(r.endpoint), language: 'typescript' }; p.source_matchers.include = ['(?-u)app\\.mts'];
  const application = await install(p, directory);
  try {
    const module = await import(pathToFileURL(path.join(directory, 'app.mts')).href);
    const instance = new module.Order(3); assert.equal(instance.value, 3); assert.equal(instance.method(4), 11);
    assert.equal(module.overloaded.length, 1); assert.equal(module.overloaded(7), 7); assert.equal(module.overloaded('seven'), 'seven');
    await assert.rejects(module.rejected(), /same/);
    const report = application.runtime.report(); assert.equal(report.function_calls, 6); assert.equal(report.functions['app.rejected'].unwinds, 1); assert.equal(report.functions['helper.helper'], undefined);
    assert.equal(fs.readFileSync(path.join(directory, 'app.mts'), 'utf8'), source);
    assert.equal(inspect(path.join(directory, 'app.mts'), p, directory).functions.filter(value => value.name === 'app.overloaded').length, 1);
  } finally { await application.close(); await r.close(); fs.rmSync(directory, { recursive: true }); }
});

test('TypeScript annotations and compiler options are explicit and validated', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-typed-policy-'));
  const p = { ...plan('http://127.0.0.1:1/v1/metrics'), language: 'typescript' }; p.function_matchers.include = ['(?-u)never'];
  try {
    const source = '// otelc.instrument\nfunction selected(value:number){return value;}\n// otelc.exclude\nfunction excluded(){return 1;}';
    const prepared = transpile(source, path.join(directory, 'a.cts'), 'a.cts', p, directory);
    const output = transform(prepared.code, path.join(directory, 'a.cts'), 'a.cts', p, { register: () => true }, prepared);
    assert.deepEqual(output.functions.map(value => value.selected), [true, false]); assert.equal(output.format, 'commonjs');
    assert.throws(() => transpile('// otelc.unknown\nfunction f(){}', path.join(directory, 'a.ts'), 'a.ts', p, directory), /unsupported otelc annotation/);
    for (const options of [{ paths: { '@app/*': ['./*'] } }, { target: 'es5' }, { module: 'amd' }, { jsx: 'preserve' }, { emitDecoratorMetadata: true }, { outFile: 'bundle.js' }]) {
      fs.writeFileSync(path.join(directory, 'tsconfig.json'), JSON.stringify({ compilerOptions: options }));
      assert.throws(() => compilerOptions(directory), /unsupported|ES2018/);
    }
    fs.writeFileSync(path.join(directory, 'tsconfig.json'), '{'); assert.throws(() => compilerOptions(directory), /expected/);
    fs.writeFileSync(path.join(directory, 'tsconfig.json'), JSON.stringify({ compilerOptions: { unknownOption: true } })); assert.throws(() => compilerOptions(directory), /Unknown compiler option/);
    fs.writeFileSync(path.join(directory, 'tsconfig.json'), JSON.stringify({ compilerOptions: { target: 'ES2022', experimentalDecorators: true }, include: ['*.ts'] }));
    assert.equal(compilerOptions(directory).experimentalDecorators, true);
    fs.writeFileSync(path.join(directory, 'tsconfig.json'), JSON.stringify({ compilerOptions: { target: 'ES5' } }));
    await assert.rejects(install(p, directory), /ES2018/);
  } finally { fs.rmSync(directory, { recursive: true }); }
});

test('unchanged and annotated TypeScript examples agree with compiler-only baseline and export metrics', async () => {
  const cli = path.join(root, 'target/debug/quux-otelc');
  const r = await receiver(); const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-typed-launch-'));
  const config = path.join(directory, 'policy.toml');
  fs.writeFileSync(config, fs.readFileSync(path.join(root, 'examples/typescript.toml'), 'utf8').replace('http://127.0.0.1:4318', r.endpoint.replace('/v1/metrics', '')));
  try {
    for (const [filename, expected, count] of [['typescript_app.mts', '75', 18], ['typescript_annotated.cts', '30', 2]]) {
      const source = path.join(root, 'examples/apps', filename), original = fs.readFileSync(source);
      const report = path.join(directory, 'report.json');
      const plain = await child(process.execPath, ['--import', path.join(root, 'adapters/node/plain.mjs'), source]);
      assert.equal(plain.status, 0, plain.stderr); assert.equal(plain.stdout.trim(), expected);
      const instrumented = await child(cli, ['--config', config, 'ts', source], { OTELC_REPORT_PATH: report });
      assert.equal(instrumented.status, 0, instrumented.stderr); assert.equal(instrumented.stdout, plain.stdout);
      const telemetry = JSON.parse(fs.readFileSync(report)); assert.equal(telemetry.function_calls, count); assert.equal(telemetry.export_loss, 0); assert.equal(telemetry.export_finished, true); assert.equal(Object.values(telemetry.losses).reduce((a, b) => a + b, 0), 0);
      assert.deepEqual(fs.readFileSync(source), original);
    }
    const doctor = await child(cli, ['--config', config, '--language', 'typescript', 'doctor']); assert.equal(doctor.status, 0, doctor.stderr); assert.match(doctor.stdout, /typescript/);
    const inventory = await child(cli, ['--config', config, '--language', 'typescript', 'inspect', 'examples/apps/typescript_annotated.cts', '--json']); assert.equal(inventory.status, 0, inventory.stderr); assert.equal(JSON.parse(inventory.stdout).functions.filter(value => value.selected).length, 2);
    const wrong = await child(cli, ['--config', config, '--language', 'typescript', 'node', 'examples/apps/typescript_app.mts']); assert.notEqual(wrong.status, 0);
    const throwing = path.join(root, 'build', 'typescript-source-map.mts');
    fs.mkdirSync(path.dirname(throwing), { recursive: true });
    fs.writeFileSync(throwing, 'type Value = number;\nexport function fail(value:Value):never {\n  throw new Error("mapped");\n}\nfail(1);\n');
    try {
      const mappedPolicy = path.join(directory, 'mapped.toml');
      fs.writeFileSync(mappedPolicy, fs.readFileSync(config, 'utf8').replace('examples/apps/typescript*.*', 'build/typescript-source-map.mts').replace('examples.apps.typescript_app.*', 'build.typescript-source-map.*'));
      const mapped = await child(cli, ['--config', mappedPolicy, 'ts', throwing]);
      assert.notEqual(mapped.status, 0); assert.match(mapped.stderr, /typescript-source-map\.mts:3:/);
    } finally { fs.unlinkSync(throwing); }
  } finally { await r.close(); fs.rmSync(directory, { recursive: true }); }
});
