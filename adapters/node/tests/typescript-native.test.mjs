import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { test } from 'node:test';
import { TraceMap, originalPositionFor } from '@jridgewell/trace-mapping';
import { JsonTraceSerializer } from '@opentelemetry/otlp-transformer';
import { nativeEmit, nativeVersion } from '../typescript-native.mjs';
import { compilerOptions, transpile } from '../typescript.mjs';
import { transform } from '../transform.mjs';
import { install } from '../register.mjs';
import { plan, receiver, child, root } from './helpers.mjs';

const nativePlan = endpoint => ({ ...plan(endpoint), language: 'typescript', backend: 'native' });

test('native compiler preserves original typed identities, annotations and source-map positions', () => {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-native-typescript-test-'));
  try {
    const p = nativePlan('http://127.0.0.1:1/v1/metrics');
    const source = '// otelc.instrument\nexport function selected(value: number): number {\n  return value + 1;\n}\n// otelc.exclude\nexport function excluded() {return 0;}';
    const filename = path.join(folder, 'original.mts'); fs.writeFileSync(filename, source);
    p.function_matchers.include = ['(?-u)nothing'];
    const prepared = transpile(source, filename, 'original.mts', p, folder);
    const transformed = transform(prepared.code, filename, 'original.mts', p, { register: () => true }, prepared);
    assert.deepEqual(transformed.functions.map(value => [value.name, value.line, value.selected]), [['original.selected', 2, true], ['original.excluded', 6, false]]);
    const lines = prepared.code.split('\n'), line = lines.findIndex(value => value.includes('return value + 1'));
    const position = originalPositionFor(new TraceMap(prepared.map), { line: line + 1, column: lines[line].indexOf('return') });
    assert.equal(position.source, filename); assert.equal(position.line, 3); assert.equal(position.column, 2);
    assert.deepEqual(prepared.map.sourcesContent, [source]); assert.equal(fs.readFileSync(filename, 'utf8'), source);
    assert.equal(nativeVersion(), '7.0.2');
  } finally {fs.rmSync(folder, { recursive: true });}
});

test('native diagnostics and launch failures leave original files and no private output', () => {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-native-typescript-fault-'));
  const p = nativePlan('http://127.0.0.1:1/v1/metrics');
  try {
    const filename = path.join(folder, 'broken.mts'), source = 'export function broken( {';
    fs.writeFileSync(filename, source);
    assert.throws(() => transpile(source, filename, 'broken.mts', p, folder), /identity parser/);
    assert.equal(fs.readFileSync(filename, 'utf8'), source);
    const previousTemporary = process.env.TMPDIR;
    try {
      process.env.TMPDIR = folder;
      assert.throws(() => transpile('export function safe(){return 1;}', filename, 'broken.mts', p, folder), /outside the source project/);
      assert.deepEqual(fs.readdirSync(folder), ['broken.mts']);
    } finally {if(previousTemporary === undefined) delete process.env.TMPDIR; else process.env.TMPDIR = previousTemporary;}
    let generated;
    const failure = (_executable, args, options) => {assert.equal(options.timeout, 30000); generated = path.dirname(args.at(-1));return { status: 1, stdout: 'compiler diagnostic', stderr: '' };};
    assert.throws(() => nativeEmit('export function works(){return 1;}', filename, 'broken.mts', p, compilerOptions(folder), failure), /native emission failed/);
    assert.ok(generated); assert.equal(fs.existsSync(generated), false);
    const omitIdentity = (_executable,args) => {
      const directory=path.dirname(args.at(-1)),output=path.join(directory,'emitted');
      fs.mkdirSync(output);fs.writeFileSync(path.join(output,'broken.mjs'),'export function works(){return 1;}');
      return {status:0};
    };
    assert.throws(() => nativeEmit('export function works(){return 1;}',filename,'broken.mts',p,compilerOptions(folder),omitIdentity), /lost an original function identity/);
    assert.throws(() => nativeVersion(() => ({status:0, stdout:'Version 8.0.0'})), /pinned executable/);
    assert.throws(() => nativeVersion(() => ({error:new Error('timeout')})), /pinned executable/);
  } finally {fs.rmSync(folder, {recursive:true});}
});

test('native loader qualifies imported types, class fields, decorators and async spans without source edits', async () => {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-native-typescript-loader-'));
  const metrics = await receiver(), traces = await receiver();
  const p = nativePlan(metrics.endpoint);
  p.export.max_queued_batches = 8;
  p.traces = {enabled:true, root_sample_ratio:1, max_active_traces:8, max_spans_per_trace:32};
  p.trace_export = {endpoint:traces.endpoint, protocol:'http/protobuf', timeout_ms:300};
  p.function_matchers.include = ['(?-u)app\\.parent', '(?-u)helper\\.child'];
  const helper = 'export enum Values { Factor = 2 }\nexport function child(value:number):number {return value*Values.Factor;}';
  const source = "import {child} from './helper.mts';\nfunction decorate(value:any){return value;}\n@decorate class Box {constructor(public value:number){}}\nexport async function parent(value:number){await Promise.resolve();return child(new Box(value).value)+1;}";
  const config = JSON.stringify({compilerOptions:{rewriteRelativeImportExtensions:true,allowImportingTsExtensions:true,noEmit:true,outDir:'application-output',incremental:true,tsBuildInfoFile:'application-cache.tsbuildinfo'}});
  fs.writeFileSync(path.join(folder,'tsconfig.json'),config);
  fs.writeFileSync(path.join(folder, 'helper.mts'), helper); fs.writeFileSync(path.join(folder, 'app.mts'), source);
  let application; const trees = [];
  try {
    application = await install(p, folder);
    const exportSpans = application.runtime.traces.exporter.export.bind(application.runtime.traces.exporter);
    application.runtime.traces.exporter.export = (spans, deadline) => {trees.push(JSON.parse(Buffer.from(JsonTraceSerializer.serializeRequest(spans)).toString()));return exportSpans(spans, deadline);};
    const api = await import(pathToFileURL(path.join(folder, 'app.mts')).href);
    assert.equal(await api.parent(4), 9); await application.close();
    const spans = trees.flatMap(tree => tree.resourceSpans.flatMap(resource => resource.scopeSpans.flatMap(scope => scope.spans)));
    assert.equal(spans.length, 2); assert.equal(spans[1].parentSpanId, spans[0].spanId);
    assert.equal(application.runtime.report().function_calls, 2); assert.equal(application.runtime.report().export_loss, 0);
    assert.deepEqual(application.runtime.report().traces.losses, {});
    assert.equal(fs.readFileSync(path.join(folder, 'app.mts'), 'utf8'), source); assert.equal(fs.readFileSync(path.join(folder, 'helper.mts'), 'utf8'), helper);
    assert.equal(fs.readFileSync(path.join(folder,'tsconfig.json'),'utf8'),config);
    assert.equal(fs.existsSync(path.join(folder,'application-output')),false);assert.equal(fs.existsSync(path.join(folder,'application-cache.tsbuildinfo')),false);
  } finally {await application?.close();await metrics.close();await traces.close();fs.rmSync(folder,{recursive:true});}
});

test('native compiler-only baseline qualifies unchanged ESM and annotated CommonJS examples', async () => {
  for (const [filename, expected] of [['typescript_app.mts', '75'], ['typescript_annotated.cts', '30']]) {
    const source = path.join(root, 'examples/apps', filename), original = fs.readFileSync(source);
    const environment = {...process.env, OTELC_TYPESCRIPT_BACKEND:'native'};
    const result = await child(process.execPath, ['--import', './adapters/node/plain.mjs', source], environment);
    assert.equal(result.status, 0, result.stderr);assert.equal(result.stdout.trim(), expected);assert.equal(result.stderr, '');
    assert.deepEqual(fs.readFileSync(source), original);
  }
});


test('native common-policy CLI preserves example calls, doctor version and original error locations', async () => {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-native-typescript-cli-'));
  const collector = await receiver();
  const cli = path.join(root, 'target/debug/quux-otelc'), policy = path.join(folder, 'policy.toml');
  try {
fs.writeFileSync(policy, fs.readFileSync(path.join(root, 'examples/typescript.toml'), 'utf8').replace('http://127.0.0.1:4318', collector.endpoint.replace('/v1/metrics', '')).replace('backend = "source"', 'backend = "native"'));

    for(const [filename,count] of [['typescript_app.mts',18],['typescript_annotated.cts',2]]) {
      const source = path.join(root, 'examples/apps',filename), original = fs.readFileSync(source), report = path.join(folder,'report.json');
      const plain = await child(process.execPath,['--import','./adapters/node/plain.mjs',source],{OTELC_TYPESCRIPT_BACKEND:'native'});
      const measured = await child(cli,['--config',policy,'ts',source],{OTELC_REPORT_PATH:report});
      assert.equal(plain.status,0,plain.stderr);assert.deepEqual(measured,plain);
      const health=JSON.parse(fs.readFileSync(report));assert.equal(health.function_calls,count);assert.equal(health.export_loss,0);
      assert.equal(health.export_finished,true);assert.ok(Object.values(health.losses).every(value=>value===0));
      assert.deepEqual(fs.readFileSync(source),original);
    }
    const doctor=await child(cli,['--config',policy,'--language','typescript','doctor']);
    assert.equal(doctor.status,0,doctor.stderr);assert.match(doctor.stdout,/native TypeScript 7\.0\.2 emit, identity parser 6\.0\.3/);
    const file=path.join(root, 'build/native-typescript-source-map.mts');fs.mkdirSync(path.dirname(file),{recursive:true});
    fs.writeFileSync(file,'type Value = number;\nexport function fail(value:Value):never {\n  throw new Error("mapped");\n}\nfail(1);\n');
    try {
      const mapped=path.join(folder,'mapped.toml');fs.writeFileSync(mapped,fs.readFileSync(policy,'utf8').replace('examples/apps/typescript*.*','build/native-typescript-source-map.mts').replace('examples.apps.typescript_app.*','build.native-typescript-source-map.*')+'\n[traces]\nenabled=true\n');
      const result=await child(cli,['--config',mapped,'ts',file]);assert.notEqual(result.status,0);assert.match(result.stderr,/native-typescript-source-map\.mts:3:/);
    } finally {fs.unlinkSync(file);}
  } finally {await collector.close();fs.rmSync(folder,{recursive:true});}
});


test('native identity inventory matches classic emit for named and anonymous typed bodies', () => {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'otelc-native-typescript-inventory-'));
  const source = `enum State { Open=4 } namespace Rules { export function bonus(n:number):number {return n+1;} }
function decorate(value:any,context:any){return value;}
class Order { constructor(public value:number){} @decorate method<T extends number>(value:T):number{return value+this.value;} }
export function selected(value:number):number{return new Order(State.Open).method(value)+Rules.bonus(value);}
const arrow=(value:number):number=>value*2;
const bound={get value(){return 1;},set value(value:number){}, method(value:number){return value;}};
[1,2].map((v:number)=>v+1).map((v:number)=>v*2);`;
  try {
    for (const target of ['ES2018', 'ES2022']) {
      fs.writeFileSync(path.join(folder,'tsconfig.json'),JSON.stringify({compilerOptions:{target}}));
      const p=nativePlan('http://127.0.0.1:1/v1/metrics'),filename=path.join(folder,'typed.mts');
      const inventory = backend => {
        const plan={...p,backend},prepared=transpile(source,filename,'typed.mts',plan,folder);
        return transform(prepared.code,filename,'typed.mts',plan,{register:()=>true},prepared).functions.map(({name,line,selected})=>({name,line,selected})).sort((a,b)=>a.name.localeCompare(b.name));
      };
      const classic=inventory('source'); assert.equal(classic.length,11);assert.deepEqual(inventory('native'),classic);
    }
    fs.writeFileSync(path.join(folder,'tsconfig.json'),JSON.stringify({compilerOptions:{experimentalDecorators:true}}));
    const legacy='function decorate(target:any,key:string,descriptor:any){return descriptor;}\nexport class Legacy {@decorate method(value:number){return value+1;}}';
    const prepared=transpile(legacy,path.join(folder,'legacy.mts'),'legacy.mts',nativePlan('http://127.0.0.1:1/v1/metrics'),folder);
    assert.match(prepared.code,/__decorate/);
    assert.deepEqual(transform(prepared.code,path.join(folder,'legacy.mts'),'legacy.mts',nativePlan('http://127.0.0.1:1/v1/metrics'),{register:()=>true},prepared).functions.map(value=>value.name),['legacy.decorate','legacy.Legacy.method']);
  } finally {fs.rmSync(folder,{recursive:true});}
});
